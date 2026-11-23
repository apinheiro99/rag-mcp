import pytest

from app.lmstudio import LMStudioClient


class StubLMStudio(LMStudioClient):
    def __init__(self):
        self.calls = []

    async def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.endswith("/load"):
            return {"status": "loaded", "instance_id": "model-x"}
        if path.endswith("/unload"):
            return {"instance_id": "model-x"}
        raise AssertionError(path)


@pytest.mark.asyncio
async def test_load_model_uses_native_endpoint():
    client = StubLMStudio()
    result = await client.load_model("model-x", context_length=8192)
    assert result["status"] == "loaded"
    method, path, kwargs = client.calls[0]
    assert method == "POST"
    assert path == "/api/v1/models/load"
    assert kwargs["json_body"]["model"] == "model-x"
    assert kwargs["json_body"]["context_length"] == 8192


@pytest.mark.asyncio
async def test_unload_model_uses_instance_id():
    client = StubLMStudio()
    result = await client.unload_model("model-x")
    assert result["instance_id"] == "model-x"
    method, path, kwargs = client.calls[0]
    assert method == "POST"
    assert path == "/api/v1/models/unload"
    assert kwargs["json_body"] == {"instance_id": "model-x"}


@pytest.mark.asyncio
async def test_ensure_model_loaded_skips_load_when_already_loaded():
    class LoadedStub(StubLMStudio):
        async def list_models(self):
            return [{"id": "model-x", "state": "loaded"}]

    client = LoadedStub()
    result = await client.ensure_model_loaded("model-x", context_length=8192)
    assert result["status"] == "already_loaded"
    assert result["instance_id"] == "model-x"
    assert client.calls == []


@pytest.mark.asyncio
async def test_ensure_model_loaded_calls_load_when_not_loaded():
    class UnloadedStub(StubLMStudio):
        async def list_models(self):
            return [{"id": "model-x", "state": "not-loaded"}]

    client = UnloadedStub()
    result = await client.ensure_model_loaded("model-x", context_length=8192)
    assert result["status"] == "loaded"
    assert client.calls[0][1] == "/api/v1/models/load"




class ChatCompletionStub(LMStudioClient):
    def __init__(self):
        self.calls = []

    async def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return {
            "choices": [{
                "message": {"content": "Yes"},
                "logprobs": {"content": []},
            }]
        }


@pytest.mark.asyncio
async def test_chat_completion_can_request_logprobs_for_reranker_scoring():
    client = ChatCompletionStub()
    data = await client.chat_completion(
        [{"role": "user", "content": "query"}],
        "qwen3-reranker-0.6b",
        temperature=0.0,
        max_tokens=1,
        timeout=30,
        reasoning_effort="none",
        logprobs=True,
        top_logprobs=20,
    )
    assert data["choices"][0]["message"]["content"] == "Yes"
    method, path, kwargs = client.calls[0]
    assert method == "POST"
    assert path == "/v1/chat/completions"
    payload = kwargs["json_body"]
    assert payload["model"] == "qwen3-reranker-0.6b"
    assert payload["max_tokens"] == 1
    assert payload["logprobs"] is True
    assert payload["top_logprobs"] == 20


