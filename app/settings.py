"""Settings em runtime: defaults.yaml + overrides persistidos no Postgres.

Tudo que a UI altera (URL do LM Studio, modelos, defaults de ingestão/busca)
vive aqui — nada é hardcoded nos módulos consumidores.
"""
from __future__ import annotations

import copy
import logging
from typing import Any

from .config import deep_merge, env, load_defaults
from .db import db
from .lmstudio import LMStudioClient

log = logging.getLogger("rag.settings")

SETTINGS_KEY = "runtime"
PAUSE_KEY = "worker_paused"
DEPLOY_PAUSE_KEY = "deploy_worker_paused"


class SettingsService:
    def __init__(self) -> None:
        self._defaults = load_defaults(env.defaults_file)
        self._cache: dict[str, Any] | None = None

    async def load(self) -> None:
        stored = await db.get_setting(SETTINGS_KEY, {}) or {}
        changed = False
        if "lmstudio_url" not in stored:
            stored["lmstudio_url"] = env.lmstudio_url_default
            changed = True
        if "reranker_model" not in stored:
            stored["reranker_model"] = self._defaults.get("rerank", {}).get("model")
            changed = True
        if changed:
            await db.set_setting(SETTINGS_KEY, stored)
        self._cache = stored

    async def raw(self) -> dict:
        if self._cache is None:
            await self.load()
        return copy.deepcopy(self._cache or {})

    async def update(self, patch: dict) -> dict:
        current = await self.raw()
        allowed_top = {"lmstudio_url", "embedding_model", "llm_model", "reranker_model", "overrides"}
        for k in patch:
            if k not in allowed_top:
                raise ValueError(f"chave de settings desconhecida: {k}")
        if "lmstudio_url" in patch:
            patch["lmstudio_url"] = str(patch["lmstudio_url"]).rstrip("/")
        if "overrides" in patch and isinstance(patch["overrides"], dict):
            current["overrides"] = deep_merge(current.get("overrides", {}), patch["overrides"])
            patch = {k: v for k, v in patch.items() if k != "overrides"}
        current.update(patch)
        await db.set_setting(SETTINGS_KEY, current)
        self._cache = current
        return copy.deepcopy(current)

    async def effective(self) -> dict:
        """defaults.yaml + overrides da UI (camada de processamento)."""
        raw = await self.raw()
        return deep_merge(self._defaults, raw.get("overrides", {}))

    async def get(self, key: str, default: Any = None) -> Any:
        raw = await self.raw()
        return raw.get(key, default)

    async def lmstudio_client(self, *, timeout: float | None = None,
                              retries: int | None = None) -> LMStudioClient:
        eff = await self.effective()
        ing = eff["ingest"]
        return LMStudioClient(
            await self.get("lmstudio_url", env.lmstudio_url_default),
            timeout=timeout if timeout is not None else ing["embed_timeout"],
            max_retries=retries if retries is not None else ing["embed_max_retries"],
            backoff_base=2.0, backoff_max=30.0,
        )

    async def paused(self) -> bool:
        return bool(await db.get_setting(PAUSE_KEY, False))

    async def set_paused(self, paused: bool) -> None:
        await db.set_setting(PAUSE_KEY, bool(paused))

    async def deployment_paused(self) -> bool:
        return bool(await db.get_setting(DEPLOY_PAUSE_KEY, False))

    async def set_deployment_paused(self, paused: bool) -> None:
        await db.set_setting(DEPLOY_PAUSE_KEY, bool(paused))

    async def workers_paused(self) -> bool:
        return await self.paused() or await self.deployment_paused()


settings = SettingsService()
