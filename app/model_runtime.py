"""Single-owner FIFO scheduler for LM Studio model residency and GPU work."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

log = logging.getLogger("rag.model_runtime")
T = TypeVar("T")


@dataclass(frozen=True)
class ModelRequirement:
    model: str
    context_length: int | None = None


@dataclass
class _Job:
    name: str
    client: Any
    requirements: tuple[ModelRequirement, ...]
    operation: Callable[[], Awaitable[Any]]
    future: asyncio.Future
    release_after: bool = False


class ModelRuntimeScheduler:
    """Serializes model-dependent GPU work while preserving each caller's result.

    Jobs are FIFO. Before a job runs, the scheduler reconciles LM Studio to the
    exact requested model set. Consecutive jobs with the same profile reuse the
    already-loaded models without churn.

    Design references retrieved through this RAG server itself:
    - Unmesh Joshi, *Patterns of Distributed Systems*, Chapter 13
      "Singular Update Queue": single-consumer ordering for asynchronous work.
    - Michael T. Nygard, *Release It!*, "Create Back Pressure": bound queues
      so producers cannot consume unbounded memory while a finite consumer lags.

    Those references motivate the single-owner FIFO + bounded-queue design; the
    implementation here is specific to LM Studio model residency and VRAM.
    """

    def __init__(self, maxsize: int = 32) -> None:
        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=maxsize)
        self._maxsize = maxsize
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._residency_lock = asyncio.Lock()
        self.current_job: str | None = None
        self.current_models: tuple[str, ...] = ()

    async def configure(self, *, maxsize: int) -> None:
        if self._task and not self._task.done() and maxsize != self._maxsize:
            raise RuntimeError("model runtime queue size cannot change while scheduler is running")
        if maxsize < 1:
            raise ValueError("model runtime queue_maxsize must be >= 1")
        if maxsize != self._maxsize:
            self._queue = asyncio.Queue(maxsize=maxsize)
            self._maxsize = maxsize

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            # Test runners and embedding hosts may create multiple event loops over
            # this process lifetime. Runtime jobs never migrate between loops. An
            # actually running old loop is a programming error; a stopped/retired
            # loop is safe to discard and rebuild around the current loop.
            if self._loop.is_running():
                raise RuntimeError("model runtime scheduler is active on another event loop")
            self._queue = asyncio.Queue(maxsize=self._maxsize)
            self._stop = asyncio.Event()
            self._task = None
            self.current_job = None
            self._residency_lock = asyncio.Lock()
        self._loop = loop
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._worker(), name="model-runtime-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self.current_job = None
        self._loop = None
        self._queue = asyncio.Queue(maxsize=self._maxsize)
        self._stop = asyncio.Event()


    async def ensure_resident(
        self,
        client: Any,
        requirements: list[ModelRequirement] | tuple[ModelRequirement, ...],
        *,
        exact: bool = False,
    ) -> None:
        """Ensure required models are loaded; optionally evict models outside the profile."""
        async with self._residency_lock:
            loaded = await client.list_models()
            loaded_ids = {
                str(item.get("id") or "")
                for item in loaded
                if item.get("state") == "loaded" and item.get("id")
            }
            desired = {req.model for req in requirements if req.model}
            if exact:
                for model in sorted(loaded_ids - desired):
                    await client.unload_model(model)
                    loaded_ids.discard(model)
            for req in requirements:
                if req.model and req.model not in loaded_ids:
                    await client.ensure_model_loaded(req.model, context_length=req.context_length)
                    loaded_ids.add(req.model)
            self.current_models = tuple(sorted(loaded_ids))

    async def run(
        self,
        name: str,
        client: Any,
        requirements: list[ModelRequirement] | tuple[ModelRequirement, ...],
        operation: Callable[[], Awaitable[T]],
        *,
        release_after: bool = False,
    ) -> T:
        await self.start()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        job = _Job(name, client, tuple(requirements), operation, future, release_after)
        await self._queue.put(job)
        try:
            return await future
        except asyncio.CancelledError:
            future.cancel()
            raise

    async def _worker(self) -> None:
        while not self._stop.is_set():
            job = await self._queue.get()
            if job.future.cancelled():
                self._queue.task_done()
                continue
            self.current_job = job.name
            try:
                await self._reconcile(job.client, job.requirements)
                try:
                    result = await job.operation()
                finally:
                    if job.release_after:
                        try:
                            await self._reconcile(job.client, ())
                        except Exception:
                            log.exception("failed to release model profile after job %s", job.name)
                if not job.future.cancelled():
                    job.future.set_result(result)
            except asyncio.CancelledError:
                if not job.future.done():
                    job.future.cancel()
                raise
            except Exception as exc:
                if not job.future.cancelled():
                    job.future.set_exception(exc)
            finally:
                self.current_job = None
                self._queue.task_done()

    async def _reconcile(self, client: Any, requirements: tuple[ModelRequirement, ...]) -> None:
        desired = {r.model for r in requirements if r.model}
        loaded = await client.list_models()
        loaded_ids = {
            str(item.get("id") or "")
            for item in loaded
            if item.get("state") == "loaded" and item.get("id")
        }

        for model in sorted(loaded_ids - desired):
            await client.unload_model(model)

        for req in requirements:
            if req.model not in loaded_ids:
                await client.ensure_model_loaded(req.model, context_length=req.context_length)

        self.current_models = tuple(req.model for req in requirements if req.model)

    def status(self) -> dict[str, Any]:
        return {
            "queued": self._queue.qsize(),
            "current_job": self.current_job,
            "current_models": list(self.current_models),
            "maxsize": self._maxsize,
        }


runtime_scheduler = ModelRuntimeScheduler()
