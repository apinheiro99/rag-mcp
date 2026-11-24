import asyncio
import pytest

from app.model_runtime import ModelRequirement, ModelRuntimeScheduler


class FakeClient:
    def __init__(self, loaded=()):
        self.loaded = set(loaded)
        self.calls = []

    async def list_models(self):
        return [{"id": m, "state": "loaded"} for m in sorted(self.loaded)]

    async def unload_model(self, model):
        self.calls.append(("unload", model))
        self.loaded.discard(model)
        return {"status": "ok"}

    async def ensure_model_loaded(self, model, *, context_length=None):
        self.calls.append(("load", model, context_length))
        self.loaded.add(model)
        return {"instance_id": model}


@pytest.mark.asyncio
async def test_scheduler_reuses_same_profile_without_model_churn():
    scheduler = ModelRuntimeScheduler()
    client = FakeClient(["embedding", "reranker"])
    seen = []
    try:
        req = [ModelRequirement("embedding", 8192), ModelRequirement("reranker", 8192)]
        await scheduler.run("a", client, req, lambda: _record(seen, "a"))
        await scheduler.run("b", client, req, lambda: _record(seen, "b"))
        assert seen == ["a", "b"]
        assert client.calls == []
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_scheduler_switches_exact_profile_and_preserves_fifo_results():
    scheduler = ModelRuntimeScheduler()
    client = FakeClient(["embedding", "reranker"])
    gate = asyncio.Event()
    order = []

    async def first():
        order.append("first-start")
        await gate.wait()
        order.append("first-end")
        return "A"

    async def second():
        order.append("second")
        return "B"

    try:
        a = asyncio.create_task(scheduler.run("classify", client, [ModelRequirement("gemma", 8192)], first))
        await asyncio.sleep(0)
        b = asyncio.create_task(scheduler.run("search", client, [ModelRequirement("embedding"), ModelRequirement("reranker")], second))
        await asyncio.sleep(0)
        gate.set()
        assert await a == "A"
        assert await b == "B"
        assert order == ["first-start", "first-end", "second"]
        assert client.calls[:3] == [("unload", "embedding"), ("unload", "reranker"), ("load", "gemma", 8192)]
        assert client.calls[-3:] == [("unload", "gemma"), ("load", "embedding", None), ("load", "reranker", None)]
    finally:
        await scheduler.stop()


async def _record(seen, value):
    seen.append(value)
    return value


@pytest.mark.asyncio
async def test_bounded_queue_applies_backpressure():
    scheduler = ModelRuntimeScheduler(maxsize=1)
    client = FakeClient([])
    first_gate = asyncio.Event()
    second_gate = asyncio.Event()

    async def first():
        await first_gate.wait()
        return "first"

    async def second():
        await second_gate.wait()
        return "second"

    try:
        t1 = asyncio.create_task(scheduler.run("first", client, [], first))
        await asyncio.sleep(0.01)
        t2 = asyncio.create_task(scheduler.run("second", client, [], second))
        await asyncio.sleep(0.01)
        t3 = asyncio.create_task(scheduler.run("third", client, [], lambda: _record([], "third")))
        await asyncio.sleep(0.01)
        assert not t3.done(), "third producer must wait while bounded queue is full"
        first_gate.set()
        assert await t1 == "first"
        await asyncio.sleep(0.01)
        second_gate.set()
        assert await t2 == "second"
        assert await t3 == "third"
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_release_after_unloads_profile_even_when_operation_fails():
    scheduler = ModelRuntimeScheduler()
    client = FakeClient(["embedding"])

    async def fail():
        raise RuntimeError("boom")

    try:
        with pytest.raises(RuntimeError, match="boom"):
            await scheduler.run(
                "classify", client, [ModelRequirement("gemma", 8192)], fail, release_after=True
            )
        assert client.loaded == set()
        assert ("unload", "gemma") in client.calls
    finally:
        await scheduler.stop()

@pytest.mark.asyncio
async def test_ensure_resident_loads_missing_without_unloading_extra_models():
    scheduler = ModelRuntimeScheduler()
    client = FakeClient(["embedding", "other"])
    try:
        await scheduler.ensure_resident(
            client,
            [ModelRequirement("embedding"), ModelRequirement("reranker", 8192)],
        )
        assert client.loaded == {"embedding", "other", "reranker"}
        assert ("unload", "other") not in client.calls
        assert ("load", "reranker", 8192) in client.calls
    finally:
        await scheduler.stop()
