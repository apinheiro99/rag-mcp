from __future__ import annotations

import logging
import time
from typing import Any

from .model_runtime import ModelRequirement, runtime_scheduler
from .rerank import rerank_results
from .settings import settings

log = logging.getLogger("rag.rerank_runtime")


async def rerank_with_runtime(
    query: str,
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
    client: Any,
    embedding_models: list[str],
) -> tuple[list[dict[str, Any]], float, str | None]:
    if not config.get("enabled") or not candidates:
        return candidates, 0.0, None

    model = await settings.get("reranker_model", config.get("model"))
    cfg = {**config, "model": model}
    started = time.perf_counter()

    try:
        context_length = int(cfg.get("context_length", 8192))
        requirements = [ModelRequirement(m, context_length) for m in embedding_models]
        requirements.append(ModelRequirement(model, context_length))
        await runtime_scheduler.ensure_resident(client, requirements, exact=True)
        ranked = await rerank_results(query, candidates, cfg, client)
        return ranked, (time.perf_counter() - started) * 1000, None
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if not cfg.get("fail_open", True):
            raise
        log.warning("rerank failed; using vector ranking: %s", error)
        return candidates, (time.perf_counter() - started) * 1000, error
