import pytest

from app.model_runtime import ModelRequirement
from app.rerank_runtime import rerank_with_runtime


@pytest.mark.asyncio
async def test_rerank_runtime_fails_open_to_vector_candidates(monkeypatch):
    async def fake_get(key, default=None):
        return "broken-model" if key == "reranker_model" else default

    async def fake_ensure(client, requirements):
        raise RuntimeError("reranker unavailable")

    monkeypatch.setattr("app.rerank_runtime.settings.get", fake_get)
    monkeypatch.setattr("app.rerank_runtime.runtime_scheduler.ensure_resident", fake_ensure)
    candidates = [
        {"chunk_id": "a", "score": 0.9, "content": "first"},
        {"chunk_id": "b", "score": 0.8, "content": "second"},
    ]
    ranked, duration_ms, error = await rerank_with_runtime(
        "query", candidates,
        {"enabled": True, "mode": "resident", "fail_open": True},
        object(), ["embedding-model"],
    )
    assert ranked == candidates
    assert duration_ms >= 0
    assert "reranker unavailable" in error


@pytest.mark.asyncio
async def test_rerank_runtime_requests_embedding_plus_selected_reranker(monkeypatch):
    captured = {}

    async def fake_get(key, default=None):
        return "qwen3-reranker-4b" if key == "reranker_model" else default

    async def fake_ensure(client, requirements):
        captured["requirements"] = requirements

    async def fake_rerank(query, candidates, config, client):
        return candidates

    monkeypatch.setattr("app.rerank_runtime.settings.get", fake_get)
    monkeypatch.setattr("app.rerank_runtime.runtime_scheduler.ensure_resident", fake_ensure)
    monkeypatch.setattr("app.rerank_runtime.rerank_results", fake_rerank)

    candidates = [{"chunk_id": "a", "score": 0.9, "content": "first"}]
    ranked, _, error = await rerank_with_runtime(
        "query", candidates,
        {"enabled": True, "fail_open": False, "context_length": 8192},
        object(), ["text-embedding-qwen3-embedding-8b"],
    )
    assert ranked == candidates
    assert error is None
    assert captured["requirements"] == [
        ModelRequirement("text-embedding-qwen3-embedding-8b", 8192),
        ModelRequirement("qwen3-reranker-4b", 8192),
    ]


@pytest.mark.asyncio
async def test_disabled_rerank_does_not_enqueue_gpu_job(monkeypatch):
    async def fail_run(*args, **kwargs):
        raise AssertionError("scheduler must not be called")

    monkeypatch.setattr("app.rerank_runtime.runtime_scheduler.run", fail_run)
    candidates = [{"chunk_id": "a", "score": 0.9, "content": "first"}]
    ranked, duration_ms, error = await rerank_with_runtime(
        "query", candidates, {"enabled": False}, object(), ["embedding-model"]
    )
    assert ranked == candidates
    assert duration_ms == 0
    assert error is None
