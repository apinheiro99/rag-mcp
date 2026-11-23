"""Cliente do LM Studio (API OpenAI-compatible + API nativa REST).

- Lista modelos: tenta /api/v0/models (nativa, informa tipo) e cai para
  /v1/models (OpenAI) se indisponível.
- Embeddings: /v1/embeddings com retries/backoff para falhas transitórias;
  fallback item-a-item para modelos que não aceitam entrada em lote.
- Chat: /v1/chat/completions (usado pelo modo automático de configuração).

Falhas transitórias levantam LMStudioUnavailable (o pipeline pausa o
documento e tenta de novo mais tarde, sem perder progresso).
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse
from typing import Any

import httpx

log = logging.getLogger("rag.lmstudio")


class LMStudioError(Exception):
    """Erro permanente (ex.: modelo inexistente, resposta inválida)."""


class LMStudioUnavailable(LMStudioError):
    """Erro transitório: servidor fora do ar, timeout, 5xx, 429."""


def _is_transient_status(status: int) -> bool:
    return status == 429 or status >= 500 or status == 408


class LMStudioClient:
    def __init__(self, base_url: str, timeout: float = 180.0, max_retries: int = 4,
                 backoff_base: float = 2.0, backoff_max: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max

    async def _request(self, method: str, path: str, *, json_body: dict | None = None,
                       timeout: float | None = None, retries: int | None = None) -> dict:
        url = self.base_url + path
        attempts = retries if retries is not None else self.max_retries
        last_err: Exception | None = None
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
                    resp = await client.request(method, url, json=json_body)
                if resp.status_code >= 400:
                    body = resp.text[:500]
                    if _is_transient_status(resp.status_code):
                        raise LMStudioUnavailable(f"HTTP {resp.status_code} de {url}: {body}")
                    raise LMStudioError(f"HTTP {resp.status_code} de {url}: {body}")
                return resp.json()
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = LMStudioUnavailable(f"falha de conexão com {url}: {e}")
            except LMStudioUnavailable as e:
                last_err = e
            except LMStudioError:
                raise
            except ValueError as e:
                raise LMStudioError(f"resposta não-JSON de {url}: {e}") from e
            if attempt < attempts - 1:
                delay = min(self.backoff_base * (2 ** attempt), self.backoff_max)
                log.warning("LM Studio transitório (%s); retry %d/%d em %.1fs",
                            last_err, attempt + 1, attempts - 1, delay)
                await asyncio.sleep(delay)
        raise last_err or LMStudioUnavailable(f"LM Studio indisponível: {url}")

    # ---------- modelos ----------
    async def list_models(self) -> list[dict[str, Any]]:
        """Retorna [{id, type, ...}]; type ∈ embedding|llm|unknown."""
        try:
            data = await self._request("GET", "/api/v0/models", timeout=15, retries=1)
            models = []
            for m in data.get("data", []):
                mtype = m.get("type", "")
                kind = "embedding" if mtype == "embeddings" else ("llm" if mtype in ("llm", "vlm") else "unknown")
                models.append({
                    "id": m.get("id"),
                    "type": kind,
                    "state": m.get("state"),
                    "arch": m.get("arch"),
                    "publisher": m.get("publisher"),
                    "max_context_length": m.get("max_context_length"),
                    "source": "native",
                })
            if models:
                return models
        except LMStudioError as e:
            log.info("API nativa do LM Studio indisponível (%s); usando /v1/models", e)
        data = await self._request("GET", "/v1/models", timeout=15, retries=2)
        return [{"id": m.get("id"), "type": "unknown", "source": "openai"}
                for m in data.get("data", []) if m.get("id")]

    async def health(self) -> dict[str, Any]:
        parsed = urlparse(self.base_url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not host:
            return {"reachable": False, "url": self.base_url, "error": "URL inválida"}
        writer = None
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=min(float(self.timeout), 5.0),
            )
            return {"reachable": True, "url": self.base_url}
        except Exception as e:
            return {"reachable": False, "url": self.base_url, "error": str(e)}
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    async def load_model(self, model: str, *, context_length: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model}
        if context_length is not None:
            payload["context_length"] = int(context_length)
        return await self._request(
            "POST", "/api/v1/models/load", json_body=payload,
            timeout=max(float(getattr(self, "timeout", 180.0)), 180.0), retries=1,
        )

    async def unload_model(self, instance_id: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/api/v1/models/unload",
            json_body={"instance_id": instance_id},
            timeout=max(float(getattr(self, "timeout", 60.0)), 60.0), retries=1,
        )

    async def ensure_model_loaded(self, model: str, *, context_length: int | None = None) -> dict[str, Any]:
        for item in await self.list_models():
            if item.get("id") == model and item.get("state") == "loaded":
                return {"status": "already_loaded", "instance_id": model}
        return await self.load_model(model, context_length=context_length)

    # ---------- embeddings ----------
    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        if not texts:
            return []
        payload = {"model": model, "input": texts}
        try:
            data = await self._request("POST", "/v1/embeddings", json_body=payload)
            return self._parse_embeddings(data, len(texts))
        except LMStudioError as e:
            if isinstance(e, LMStudioUnavailable) or len(texts) == 1:
                raise
            # Alguns modelos não aceitam lote: tenta item a item.
            log.warning("lote de %d textos falhou (%s); tentando item a item", len(texts), e)
            out: list[list[float]] = []
            for t in texts:
                data = await self._request("POST", "/v1/embeddings",
                                           json_body={"model": model, "input": t})
                out.extend(self._parse_embeddings(data, 1))
            return out

    @staticmethod
    def _parse_embeddings(data: dict, expected: int) -> list[list[float]]:
        items = data.get("data")
        if not isinstance(items, list) or len(items) != expected:
            raise LMStudioError(f"resposta de embeddings inválida (esperava {expected} vetores)")
        items = sorted(items, key=lambda x: x.get("index", 0))
        vectors = []
        dim = None
        for it in items:
            vec = it.get("embedding")
            if not isinstance(vec, list) or not vec:
                raise LMStudioError("resposta de embeddings sem vetor")
            if dim is None:
                dim = len(vec)
            elif len(vec) != dim:
                raise LMStudioError("dimensões inconsistentes na resposta de embeddings")
            vectors.append([float(x) for x in vec])
        return vectors

    # ---------- chat (modo automático + reranker) ----------
    async def chat_completion(
        self, messages: list[dict], model: str, *, temperature: float = 0.2,
        max_tokens: int = 1500, timeout: float | None = None,
        reasoning_effort: str | None = None, logprobs: bool | None = None,
        top_logprobs: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        if logprobs is not None:
            payload["logprobs"] = bool(logprobs)
        if top_logprobs is not None:
            payload["top_logprobs"] = int(top_logprobs)
        return await self._request(
            "POST", "/v1/chat/completions", json_body=payload, timeout=timeout
        )

    async def chat(self, messages: list[dict], model: str, *, temperature: float = 0.2,
                   max_tokens: int = 1500, timeout: float | None = None,
                   reasoning_effort: str | None = None) -> str:
        data = await self.chat_completion(
            messages, model, temperature=temperature, max_tokens=max_tokens,
            timeout=timeout, reasoning_effort=reasoning_effort,
        )
        try:
            choices = data["choices"]
            msg = choices[0]["message"]
            content = msg.get("content") or ""
            if not content and msg.get("reasoning_content"):
                content = msg["reasoning_content"]
            return content
        except (KeyError, IndexError, TypeError) as e:
            raise LMStudioError(f"resposta de chat inválida: {e}") from e
