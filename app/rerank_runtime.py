from __future__ import annotations

import logging
import time
from typing import Any

from .lmstudio import model_runtime_lock
from .rerank import rerank_results
from .settings import settings

log = logging.getLogger('rag.rerank_runtime')


async def _reconcile_resident_models(
    client: Any,
    *,
    embedding_models: list[str],
    reranker_model: str,
    context_length: int,
) -> None:
    desired = {m for m in [*embedding_models, reranker_model] if m}

    # Older/test clients may not expose model inventory; keep backward compatibility.
    if hasattr(client, 'list_models'):
        loaded = await client.list_models()
        for item in loaded:
            model_id = str(item.get('id') or '').strip()
            if not model_id or item.get('state') != 'loaded' or model_id in desired:
                continue
            try:
                await client.unload_model(model_id)
            except Exception as exc:
                log.warning('modelo residente extra %s não descarregado: %s', model_id, exc)

    for embedding_model in embedding_models:
        await client.ensure_model_loaded(
            embedding_model,
            context_length=context_length,
        )
    await client.ensure_model_loaded(
        reranker_model,
        context_length=context_length,
    )


async def rerank_with_runtime(
    query: str,
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
    client: Any,
    embedding_models: list[str],
) -> tuple[list[dict[str, Any]], float, str | None]:
    if not config.get('enabled') or not candidates:
        return candidates, 0.0, None

    model = await settings.get('reranker_model', config.get('model'))
    cfg = {**config, 'model': model}
    started = time.perf_counter()

    try:
        if cfg.get('mode', 'switch') != 'switch':
            async with model_runtime_lock:
                await _reconcile_resident_models(
                    client,
                    embedding_models=embedding_models,
                    reranker_model=model,
                    context_length=int(cfg.get('context_length', 8192)),
                )
                ranked = await rerank_results(query, candidates, cfg, client)
            return ranked, (time.perf_counter() - started) * 1000, None

        async with model_runtime_lock:
            reranker_instance: str | None = None
            try:
                try:
                    await client.unload_model(model)
                except Exception:
                    pass
                for embedding_model in embedding_models:
                    try:
                        await client.unload_model(embedding_model)
                    except Exception as exc:
                        log.info('embedding model %s não descarregado: %s', embedding_model, exc)

                loaded = await client.load_model(
                    model,
                    context_length=int(cfg.get('context_length', 8192)),
                )
                reranker_instance = str(loaded.get('instance_id') or model)
                ranked = await rerank_results(query, candidates, cfg, client)
            finally:
                if reranker_instance:
                    try:
                        await client.unload_model(reranker_instance)
                    except Exception:
                        log.exception('falha ao descarregar reranker %s', reranker_instance)
                for embedding_model in embedding_models:
                    try:
                        await client.load_model(
                            embedding_model,
                            context_length=int(cfg.get('context_length', 8192)),
                        )
                    except Exception:
                        log.exception('falha ao restaurar embedding model %s', embedding_model)

        return ranked, (time.perf_counter() - started) * 1000, None
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        if not cfg.get('fail_open', True):
            raise
        log.warning('rerank falhou; usando ranking vetorial: %s', error)
        return candidates, (time.perf_counter() - started) * 1000, error
