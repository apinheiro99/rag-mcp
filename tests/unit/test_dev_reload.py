import httpx
import pytest
from fastapi import FastAPI

import app.api as api_module


@pytest.mark.asyncio
async def test_dev_reload_endpoint_disabled(monkeypatch):
    monkeypatch.setattr(api_module.env, "dev_reload", False)
    app = FastAPI()
    app.include_router(api_module.router)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/dev/reload")

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["token"] is None


@pytest.mark.asyncio
async def test_dev_reload_endpoint_exposes_change_token(monkeypatch):
    monkeypatch.setattr(api_module.env, "dev_reload", True)
    monkeypatch.setattr(api_module.env, "dev_reload_poll_ms", 750)
    monkeypatch.setattr(api_module, "_dev_reload_token", lambda: "123456")
    app = FastAPI()
    app.include_router(api_module.router)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/dev/reload")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "token": "123456",
        "poll_ms": 750,
    }
