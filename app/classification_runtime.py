"""Persistent post-embedding document classification workflow."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .collection_classifier import classify_document, collection_profile, representative_sample
from .config import env
from .db import db
from .extraction import ExtractedDoc, ExtractionError, extract
from .ingest import extraction_cache_path
from .library_taxonomy import COMPUTER_BOOKS
from .lmstudio import LMStudioError
from .rerank_runtime import rerank_with_runtime
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
    model = model or await settings.get("embedding_model")
    if not model:
        raise ValueError("select an embedding model before classifying")

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
        if doc["collection"] != COMPUTER_BOOKS:
            raise ValueError("automatic category classification applies only to Computer Books")
        cur = await conn.execute(
            """SELECT cat.id, cat.name, cat.description,
                      (SELECT count(*) FROM documents x WHERE x.category_id=cat.id) AS doc_count,
                      ARRAY(
                          SELECT x.filename FROM documents x
                          WHERE x.category_id=cat.id AND x.status='ready'
                          ORDER BY x.created_at, x.filename LIMIT 4
                      ) AS examples
               FROM categories cat
               WHERE cat.collection_id=%s
               ORDER BY cat.name""",
            (doc["collection_id"],),
        )
        collections = await cur.fetchall()

    extracted = await _sample_extract(doc)
    client = await settings.lmstudio_client(timeout=eff["auto"]["request_timeout"], retries=1)
    cfg = eff["classification"]
    result = await classify_document(
        client,
        model,
        filename=doc["filename"],
        source_path=doc["source_path"],
        text=extracted.full_text(),
        collections=collections,
        sample_chars=eff["auto"]["sample_chars"],
        timeout=eff["auto"]["request_timeout"],
        match_threshold=float(cfg.get("embedding_match_threshold", 0.55)),
        margin_threshold=float(cfg.get("embedding_margin_threshold", 0.03)),
    )
    if (
        collections
        and not result.get("existing_collection")
        and cfg.get("rerank_fallback_enabled", True)
    ):
        score_map = {
            str(item.get("collection")): float(item.get("score", 0.0))
            for item in result.get("candidates", [])
        }
        candidates = [
            {
                "chunk_id": str(category["id"]),
                "score": score_map.get(str(category["name"]), 0.0),
                "content": collection_profile(category),
                "category_name": str(category["name"]),
            }
            for category in collections
        ]
        query = (
            f"Filename: {doc['filename']}\n\n"
            + representative_sample(extracted.full_text(), eff["auto"]["sample_chars"])
        )
        rerank_cfg = {
            **eff["rerank"],
            "qwen_instruction": (
                "Given a document and a candidate category description, determine whether "
                "the document belongs to that category."
            ),
        }
        ranked, _, rerank_error = await rerank_with_runtime(
            query, candidates, rerank_cfg, client, [model]
        )
        if ranked and not rerank_error:
            best = ranked[0]
            second = ranked[1]["rerank_score"] if len(ranked) > 1 else 0.0
            best_score = float(best.get("rerank_score", 0.0))
            margin = best_score - float(second)
            matched = (
                best_score >= float(cfg.get("rerank_match_threshold", 0.55))
                and (
                    len(ranked) == 1
                    or margin >= float(cfg.get("rerank_margin_threshold", 0.05))
                )
            )
            if matched:
                result["existing_collection"] = best["category_name"]
                result["confidence"] = round(best_score, 4)
                result["rationale"] = (
                    f"Reranker selected existing category: {best['category_name']} "
                    f"(score={best_score:.4f}, margin={margin:.4f})."
                )
                result["source"] = "reranker_fallback"
                result["fallback_model"] = await settings.get("reranker_model")

    if not result.get("existing_collection"):
        result["proposed_collection"] = None
        result["new_collection_suggested"] = False
        result["proposal_source"] = "disabled"
    threshold = float(cfg["confidence_threshold"])
    result["existing_category"] = result.get("existing_collection")
    result["proposed_category"] = result.get("proposed_collection")
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
    """Assign an existing category without moving the document between collections."""
    eff = await settings.effective()
    cfg = eff["classification"]
    threshold = float(cfg["confidence_threshold"])
    existing_name = result.get("existing_category") or result.get("existing_collection")
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
            doc = await (
                await conn.execute(
                    """SELECT d.id,d.filename,d.status,d.collection_id,c.name AS collection
                       FROM documents d JOIN collections c ON c.id=d.collection_id
                       WHERE d.id=%s FOR UPDATE""",
                    (doc_id,),
                )
            ).fetchone()
            if not doc or doc["status"] != "ready":
                result["auto_apply_reason"] = "document is not ready"
                return result
            if doc["collection"] != COMPUTER_BOOKS:
                result["auto_apply_reason"] = "document is not in Computer Books"
                return result

            target = await (
                await conn.execute(
                    """SELECT * FROM categories
                       WHERE collection_id=%s AND name=%s""",
                    (doc["collection_id"], existing_name),
                )
            ).fetchone()
            if not target:
                result["auto_apply_reason"] = "target category no longer exists"
                return result

            result.update({
                "auto_applied": True,
                "routed_to": existing_name,
                "status": "ready",
                "existing_category": existing_name,
            })
            await conn.execute(
                """UPDATE documents SET category_id=%s,auto_suggestion=%s::jsonb,
                       meta=(coalesce(meta,'{}'::jsonb) - 'auto_classify'),updated_at=now()
                   WHERE id=%s""",
                (target["id"], json.dumps(result), doc_id),
            )

    await db.event(
        f"Automatically categorized: {doc['filename']} → {existing_name} ({float(result['confidence']):.2f})",
        collection_id=doc["collection_id"], document_id=doc_id,
    )
    return result


async def classify_ready_document(doc_id: str, model: str | None = None) -> dict[str, Any]:
    result = await classify_document_runtime(doc_id, model)
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
            if await settings.workers_paused() or not cfg.get("enabled", True):
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
