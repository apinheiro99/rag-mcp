"""Persistent post-embedding document classification workflow."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .collection_classifier import classify_document
from .config import env
from .db import db
from .extraction import ExtractedDoc, ExtractionError, extract
from .ingest import extraction_cache_path
from .lmstudio import LMStudioError
from .model_runtime import ModelRequirement, runtime_scheduler
from .settings import settings

log = logging.getLogger("rag.classification_runtime")


async def _sample_extract(doc: dict) -> ExtractedDoc:
    cache = extraction_cache_path(str(doc["id"]))
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("sha256") == doc["content_sha256"]:
                return ExtractedDoc.from_json(data)
        except Exception:
            pass
    return await asyncio.to_thread(
        extract, env.data_dir / doc["stored_path"], doc["filename"], sample=True
    )


async def classify_document_runtime(doc_id: str, model: str | None = None) -> dict[str, Any]:
    model = model or await settings.get("llm_model")
    if not model:
        raise ValueError("select an LLM model before classifying")

    eff = await settings.effective()
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT d.*, c.name AS collection, c.embedding_model, c.embedding_dim
               FROM documents d JOIN collections c ON c.id=d.collection_id
               WHERE d.id=%s""",
            (doc_id,),
        )
        doc = await cur.fetchone()
        if not doc:
            raise ValueError("document not found")
        cur = await conn.execute(
            """SELECT c.id, c.name, c.description, c.embedding_model, c.embedding_dim,
                      (SELECT count(*) FROM documents x WHERE x.collection_id=c.id) AS doc_count
               FROM collections c WHERE c.id<>%s ORDER BY c.name""",
            (doc["collection_id"],),
        )
        collections = await cur.fetchall()

    extracted = await _sample_extract(doc)
    client = await settings.lmstudio_client(timeout=eff["auto"]["request_timeout"], retries=1)
    cfg = eff["classification"]
    result = await runtime_scheduler.run(
        f"classify:{doc_id}",
        client,
        [ModelRequirement(model, int(cfg["context_length"]))],
        lambda: classify_document(
            client,
            model,
            filename=doc["filename"],
            source_path=doc["source_path"],
            text=extracted.full_text(),
            collections=collections,
            sample_chars=eff["auto"]["sample_chars"],
            timeout=eff["auto"]["request_timeout"],
            reasoning_effort=cfg.get("reasoning_effort"),
        ),
        release_after=True,
    )
    threshold = float(cfg["confidence_threshold"])
    result.update({
        "document_id": doc_id,
        "filename": doc["filename"],
        "current_collection": doc["collection"],
        "auto_apply_threshold": threshold,
        "auto_apply_eligible": bool(
            cfg["auto_apply_existing"]
            and result.get("existing_collection")
            and float(result["confidence"]) >= threshold
        ),
    })
    return result


async def apply_ready_classification(doc_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Move an already-embedded document without re-embedding when compatible."""
    eff = await settings.effective()
    cfg = eff["classification"]
    threshold = float(cfg["confidence_threshold"])
    existing_name = result.get("existing_collection")
    eligible = bool(
        cfg["auto_apply_existing"]
        and existing_name
        and float(result["confidence"]) >= threshold
    )
    result.update({
        "auto_apply_threshold": threshold,
        "auto_apply_eligible": eligible,
        "auto_applied": False,
    })
    if not eligible:
        return result

    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                """SELECT d.id,d.filename,d.status,d.content_sha256,d.collection_id,
                          src.name AS source_collection,src.embedding_model AS source_model,
                          src.embedding_dim AS source_dim
                   FROM documents d JOIN collections src ON src.id=d.collection_id
                   WHERE d.id=%s FOR UPDATE""",
                (doc_id,),
            )
            doc = await cur.fetchone()
            if not doc or doc["status"] != "ready":
                result["auto_apply_reason"] = "document is not ready"
                return result

            cur = await conn.execute("SELECT * FROM collections WHERE name=%s", (existing_name,))
            target = await cur.fetchone()
            if not target:
                result["auto_apply_reason"] = "target collection no longer exists"
                return result

            cur = await conn.execute(
                "SELECT id FROM documents WHERE collection_id=%s AND content_sha256=%s AND id<>%s",
                (target["id"], doc["content_sha256"], doc_id),
            )
            if await cur.fetchone():
                result["auto_apply_reason"] = "identical content already exists in target collection"
                return result

            if target["embedding_model"] and target["embedding_model"] != doc["source_model"]:
                result["auto_apply_reason"] = "target uses a different embedding model"
                return result
            if target["embedding_dim"] and target["embedding_dim"] != doc["source_dim"]:
                result["auto_apply_reason"] = "target uses a different embedding dimension"
                return result

            if not target["embedding_model"]:
                await conn.execute(
                    "UPDATE collections SET embedding_model=%s, embedding_dim=%s, updated_at=now() WHERE id=%s",
                    (doc["source_model"], doc["source_dim"], target["id"]),
                )

            await conn.execute(
                """UPDATE documents SET collection_id=%s,auto_suggestion=%s::jsonb,
                       meta=(coalesce(meta,'{}'::jsonb) - 'auto_classify'),updated_at=now()
                   WHERE id=%s""",
                (target["id"], json.dumps(result), doc_id),
            )
            await conn.execute(
                "UPDATE chunks SET collection_id=%s WHERE document_id=%s",
                (target["id"], doc_id),
            )

    result.update({"auto_applied": True, "routed_to": existing_name, "status": "ready"})
    await db.event(
        f"Automatically classified: {doc['filename']} → {existing_name} ({float(result['confidence']):.2f})",
        collection_id=target["id"], document_id=doc_id,
    )
    return result


async def classify_ready_document(doc_id: str) -> dict[str, Any]:
    result = await classify_document_runtime(doc_id)
    result = await apply_ready_classification(doc_id, result)
    if not result.get("auto_applied"):
        async with db.pool.connection() as conn:
            await conn.execute(
                """UPDATE documents SET auto_suggestion=%s::jsonb,
                       meta=(coalesce(meta,'{}'::jsonb) - 'auto_classify'),updated_at=now()
                   WHERE id=%s""",
                (json.dumps(result), doc_id),
            )
        await db.event(
            f"Classification review required: {result['filename']} ({float(result['confidence']):.2f})",
            document_id=doc_id,
        )
    return result


class ClassificationWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.current_document: str | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="classification-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self.current_document = None

    async def _next_document(self) -> dict | None:
        async with db.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT id,filename FROM documents
                   WHERE status='ready'
                     AND coalesce((meta->>'auto_classify')::boolean,false)=true
                     AND auto_suggestion IS NULL
                     AND (meta->>'auto_classify_next_at' IS NULL
                          OR (meta->>'auto_classify_next_at')::timestamptz <= now())
                   ORDER BY created_at LIMIT 1"""
            )
            return await cur.fetchone()

    async def _defer_or_fail(self, doc_id: str, exc: Exception, cfg: dict[str, Any]) -> None:
        backoff = [int(x) for x in cfg["retry_backoff_seconds"]]
        max_attempts = int(cfg["max_attempts"])
        async with db.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT coalesce((meta->>'auto_classify_attempts')::int,0) AS attempts FROM documents WHERE id=%s",
                (doc_id,),
            )
            row = await cur.fetchone()
            attempts = int(row["attempts"] if row else 0) + 1
            if attempts >= max_attempts:
                payload = {
                    "status": "classification_error",
                    "error": str(exc),
                    "attempts": attempts,
                }
                await conn.execute(
                    """UPDATE documents SET
                           meta=((coalesce(meta,'{}'::jsonb) - 'auto_classify' - 'auto_classify_next_at')
                                 || jsonb_build_object('auto_classify_attempts',%s)),
                           auto_suggestion=%s::jsonb,updated_at=now()
                       WHERE id=%s""",
                    (attempts, json.dumps(payload), doc_id),
                )
                await db.event(
                    f"Automatic classification stopped after {attempts} attempts: {exc}",
                    level="error", document_id=doc_id,
                )
                return

            delay = backoff[min(attempts - 1, len(backoff) - 1)]
            await conn.execute(
                """UPDATE documents SET
                       meta=(coalesce(meta,'{}'::jsonb)
                             || jsonb_build_object('auto_classify_attempts',%s,
                                                   'auto_classify_next_at',to_jsonb(now() + (%s * interval '1 second')))),
                       updated_at=now() WHERE id=%s""",
                (attempts, delay, doc_id),
            )
            await db.event(
                f"Automatic classification deferred for {delay}s after attempt {attempts}: {exc}",
                level="warning", document_id=doc_id,
            )

    async def _run(self) -> None:
        while not self._stop.is_set():
            eff = await settings.effective()
            cfg = eff["classification"]
            poll = max(1, float(cfg["poll_interval_seconds"]))
            if not cfg.get("enabled", True):
                await asyncio.sleep(poll)
                continue
            doc = await self._next_document()
            if not doc:
                await asyncio.sleep(poll)
                continue
            self.current_document = str(doc["id"])
            try:
                await classify_ready_document(self.current_document)
            except asyncio.CancelledError:
                raise
            except (LMStudioError, ExtractionError, ValueError) as exc:
                log.warning("classification deferred for %s: %s", self.current_document, exc)
                await self._defer_or_fail(self.current_document, exc, cfg)
            except Exception as exc:
                log.exception("classification failed for %s", self.current_document)
                await self._defer_or_fail(self.current_document, exc, cfg)
            finally:
                self.current_document = None


classification_worker = ClassificationWorker()
