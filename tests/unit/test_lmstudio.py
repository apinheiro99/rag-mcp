import pytest

from app.lmstudio import LMStudioClient, LMStudioError, _is_transient_status


def test_transient_http_statuses():
    assert _is_transient_status(408)
    assert _is_transient_status(429)
    assert _is_transient_status(500)
    assert not _is_transient_status(400)
    assert not _is_transient_status(404)


def test_parse_embeddings_orders_by_index():
    data = {"data": [
        {"index": 1, "embedding": [3, 4]},
        {"index": 0, "embedding": [1, 2]},
    ]}
    assert LMStudioClient._parse_embeddings(data, 2) == [[1.0, 2.0], [3.0, 4.0]]


def test_parse_embeddings_rejects_wrong_count():
    with pytest.raises(LMStudioError):
        LMStudioClient._parse_embeddings({"data": []}, 1)


def test_parse_embeddings_rejects_inconsistent_dimensions():
    with pytest.raises(LMStudioError, match="dimensões inconsistentes"):
        LMStudioClient._parse_embeddings({
            "data": [
                {"index": 0, "embedding": [1, 2]},
                {"index": 1, "embedding": [1]},
            ]
        }, 2)


@pytest.mark.asyncio
async def test_list_models_native_classification(monkeypatch):
    client = LMStudioClient("http://lm")

    async def fake_request(*args, **kwargs):
        return {"data": [
            {"id": "embed", "type": "embeddings"},
            {"id": "llm", "type": "llm"},
            {"id": "other", "type": "weird"},
        ]}

    monkeypatch.setattr(client, "_request", fake_request)
    models = await client.list_models()
    assert [m["type"] for m in models] == ["embedding", "llm", "unknown"]


@pytest.mark.asyncio
async def test_embed_empty_input_short_circuits():
    client = LMStudioClient("http://lm")
    assert await client.embed([], "model") == []


class _FakeWriter:
    def __init__(self):
        self.closed = False
        self.waited = False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.waited = True


@pytest.mark.asyncio
async def test_health_checks_connectivity_without_listing_models(monkeypatch):
    client = LMStudioClient("http://lm.example:1234")
    calls = {"open": [], "listed": 0}
    writer = _FakeWriter()

    async def fake_open_connection(host, port):
        calls["open"].append((host, port))
        return object(), writer

    async def fail_list_models():
        calls["listed"] += 1
        raise AssertionError("health must not list models")

    monkeypatch.setattr("app.lmstudio.asyncio.open_connection", fake_open_connection)
    monkeypatch.setattr(client, "list_models", fail_list_models)

    result = await client.health()

    assert result == {"reachable": True, "url": "http://lm.example:1234"}
    assert calls["open"] == [("lm.example", 1234)]
    assert calls["listed"] == 0
    assert writer.closed is True
    assert writer.waited is True


@pytest.mark.asyncio
async def test_health_reports_unreachable_without_listing_models(monkeypatch):
    client = LMStudioClient("http://lm.example:1234")

    async def fake_open_connection(host, port):
        raise OSError("connection refused")

    monkeypatch.setattr("app.lmstudio.asyncio.open_connection", fake_open_connection)

    result = await client.health()

    assert result["reachable"] is False
    assert result["url"] == "http://lm.example:1234"
    assert "connection refused" in result["error"]



@pytest.mark.asyncio
async def test_chat_completion_forwards_seed(monkeypatch):
    client = LMStudioClient("http://lm")
    captured = {}

    async def fake_request(method, path, *, json_body=None, timeout=None):
        captured["json"] = json_body
        return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setattr(client, "_request", fake_request)
    await client.chat_completion(
        [{"role": "user", "content": "classify"}],
        "model",
        temperature=0.0,
        seed=42,
    )
    assert captured["json"]["seed"] == 42
