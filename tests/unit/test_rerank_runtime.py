import pytest

from app.rerank_runtime import rerank_with_runtime


class FailingClient:
    async def ensure_model_loaded(self, model, *, context_length=None):
        raise RuntimeError("reranker unavailable")


@pytest.mark.asyncio
async def test_rerank_runtime_fails_open_to_vector_candidates(monkeypatch):
    async def fake_get(key, default=None):
        if key == "reranker_model":
            return "broken-model"
        return default

    monkeypatch.setattr("app.rerank_runtime.settings.get", fake_get)
    candidates = [
        {"chunk_id": "a", "score": 0.9, "content": "first"},
        {"chunk_id": "b", "score": 0.8, "content": "second"},
    ]
    ranked, duration_ms, error = await rerank_with_runtime(
        "query",
        candidates,
        {"enabled": True, "mode": "resident", "fail_open": True},
        FailingClient(),
        ["embedding-model"],
    )
    assert ranked == candidates
    assert duration_ms >= 0
    assert error is not None
    assert "reranker unavailable" in error


@pytest.mark.asyncio
async def test_resident_rerank_releases_model_lock_before_inference(monkeypatch):
    class ResidentClient:
        async def ensure_model_loaded(self, model, *, context_length=None):
            return {"status": "already_loaded", "instance_id": model}

    async def fake_get(key, default=None):
        if key == "reranker_model":
            return "model-x"
        return default

    async def fake_rerank(query, candidates, config, client):
        from app.lmstudio import model_runtime_lock
        assert not model_runtime_lock.locked(), "resident inference must not hold model_runtime_lock"
        return candidates

    monkeypatch.setattr("app.rerank_runtime.settings.get", fake_get)
    monkeypatch.setattr("app.rerank_runtime.rerank_results", fake_rerank)

    candidates = [{"chunk_id": "a", "score": 0.9, "content": "first"}]
    ranked, _, error = await rerank_with_runtime(
        "query",
        candidates,
        {"enabled": True, "mode": "resident", "fail_open": False, "context_length": 8192},
        ResidentClient(),
        ["embedding-model"],
    )
    assert ranked == candidates
    assert error is None



@pytest.mark.asyncio
async def test_resident_runtime_reconciles_loaded_models_to_embedding_plus_selected_reranker(monkeypatch):
    class ReconcilingClient:
        def __init__(self):
            self.unloaded = []
            self.ensured = []

        async def list_models(self):
            return [
                {"id": "text-embedding-qwen3-embedding-8b", "state": "loaded", "type": "embedding"},
                {"id": "google/gemma-4-12b-qat", "state": "loaded", "type": "llm"},
                {"id": "qwen3-reranker-4b", "state": "not-loaded", "type": "llm"},
            ]

        async def unload_model(self, model):
            self.unloaded.append(model)
            return {"instance_id": model}

        async def ensure_model_loaded(self, model, *, context_length=None):
            self.ensured.append(model)
            return {"status": "already_loaded" if model == "text-embedding-qwen3-embedding-8b" else "loaded", "instance_id": model}

    async def fake_get(key, default=None):
        if key == "reranker_model":
            return "qwen3-reranker-4b"
        return default

    async def fake_rerank(query, candidates, config, client):
        return candidates

    monkeypatch.setattr("app.rerank_runtime.settings.get", fake_get)
    monkeypatch.setattr("app.rerank_runtime.rerank_results", fake_rerank)

    client = ReconcilingClient()
    candidates = [{"chunk_id": "a", "score": 0.9, "content": "first"}]

    ranked, _, error = await rerank_with_runtime(
        "query",
        candidates,
        {"enabled": True, "mode": "resident", "fail_open": False, "context_length": 8192},
        client,
        ["text-embedding-qwen3-embedding-8b"],
    )

    assert ranked == candidates
    assert error is None
    assert client.unloaded == ["google/gemma-4-12b-qat"]
    assert set(client.ensured) == {
        "text-embedding-qwen3-embedding-8b",
        "qwen3-reranker-4b",
    }


@pytest.mark.asyncio
async def test_resident_runtime_keeps_only_correct_loaded_pair(monkeypatch):
    class PairAlreadyLoadedClient:
        def __init__(self):
            self.unloaded = []
            self.ensured = []

        async def list_models(self):
            return [
                {"id": "text-embedding-qwen3-embedding-8b", "state": "loaded", "type": "embedding"},
                {"id": "qwen3-reranker-4b", "state": "loaded", "type": "llm"},
            ]

        async def unload_model(self, model):
            self.unloaded.append(model)

        async def ensure_model_loaded(self, model, *, context_length=None):
            self.ensured.append(model)
            return {"status": "already_loaded", "instance_id": model}

    async def fake_get(key, default=None):
        if key == "reranker_model":
            return "qwen3-reranker-4b"
        return default

    async def fake_rerank(query, candidates, config, client):
        return candidates

    monkeypatch.setattr("app.rerank_runtime.settings.get", fake_get)
    monkeypatch.setattr("app.rerank_runtime.rerank_results", fake_rerank)

    client = PairAlreadyLoadedClient()
    candidates = [{"chunk_id": "a", "score": 0.9, "content": "first"}]

    await rerank_with_runtime(
        "query",
        candidates,
        {"enabled": True, "mode": "resident", "fail_open": False, "context_length": 8192},
        client,
        ["text-embedding-qwen3-embedding-8b"],
    )

    assert client.unloaded == []
    assert set(client.ensured) == {
        "text-embedding-qwen3-embedding-8b",
        "qwen3-reranker-4b",
    }

