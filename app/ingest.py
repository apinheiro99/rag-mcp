"""Worker de ingestão: pipeline persistente e retomável.

documento → extração → chunking → embeddings (LM Studio) → pgvector

Princípios:
- Estágios idempotentes: cada etapa grava progresso no Postgres; reiniciar
  o processo retoma do ponto exato (chunks já embedados nunca são refeitos).
- Falha em um documento não afeta os demais: erro fica registrado no doc.
- LM Studio fora do ar → status "waiting" + backoff; retomada automática.
- Pausa global (UI) preservada no banco; sobrevive a restart.
"""
from __future__ import annotations

import asyncio
import json
import logging
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .chunking import chunk_segments, validate_ingest_config
from .config import env
from .db import db, vector_literal
from .extraction import ExtractedDoc, ExtractionError, extract
from .lmstudio import LMStudioError, LMStudioUnavailable
from .model_runtime import ModelRequirement, runtime_scheduler
from .quality import QualityGateError, analyze_document_quality
from .settings import settings
from .graph_retrieval import rebuild_document_mentions

log = logging.getLogger("rag.ingest")

ACTIVE_STATUSES = ("pending", "extracting", "chunking", "embedding", "waiting", "cancelling")
CHUNK_NS = uuid.UUID("7c9e6679-7425-40de-944b-e07fc1f90ae7")


class DocumentCancelled(Exception):
    pass


def chunk_uuid(doc_id: str | uuid.UUID, seq: int) -> str:
    return str(uuid.uuid5(CHUNK_NS, f"{doc_id}:{seq}"))


def extraction_cache_path(doc_id: str) -> Path:
    return env.extracted_dir / f"{doc_id}.json"


async def resolve_ingest_config(collection_cfg: dict | None, override: dict | None) -> dict:
    """defaults.yaml < config da coleção < override da requisição."""
    effective = await settings.effective()
    merged = dict(effective["ingest"])
    merged.update(collection_cfg or {})
    merged.update(override or {})

    allowed = (
        "chunk_size",
        "chunk_overlap",
        "min_chunk_size",
        "strategy",
        "separators",
        "embedding_model",
        "embed_batch_size",
    )
    config = {key: merged[key] for key in allowed if key in merged}
    return validate_ingest_config(config, effective["limits"])


class IngestStore:
    """Persistência do pipeline de ingestão.

    Centraliza SQL e mantém o restante do pipeline focado em decisões de domínio.
    """

    async def claim_next(self) -> dict | None:
        async with db.pool.connection() as conn:
            cur = await conn.execute(
                f"""SELECT * FROM documents
                    WHERE status IN {ACTIVE_STATUSES}
                      AND (retry_at IS NULL OR retry_at <= now())
                    ORDER BY created_at
                    LIMIT 1"""
            )
            return await cur.fetchone()

    async def increment_attempts(self, doc_id: str) -> None:
        async with db.pool.connection() as conn:
            await conn.execute(
                "UPDATE documents SET attempts=attempts+1, updated_at=now() WHERE id=%s",
                (doc_id,),
            )

    async def set_status(
        self,
        doc_id: str,
        status: str,
        *,
        error: str | None = None,
        retry_in: float | None = None,
        **fields: Any,
    ) -> None:
        sets = ["status=%s", "error=%s", "updated_at=now()", "retry_at=%s"]
        retry_at = (
            datetime.now(timezone.utc) + timedelta(seconds=retry_in)
            if retry_in
            else None
        )
        params: list[Any] = [status, error, retry_at]

        for key, value in fields.items():
            sets.append(f"{key}=%s")
            params.append(json.dumps(value) if isinstance(value, (dict, list)) else value)

        params.append(doc_id)
        async with db.pool.connection() as conn:
            await conn.execute(
                f"UPDATE documents SET {', '.join(sets)} WHERE id=%s AND status NOT IN ('cancelling','cancelled')",
                params,
            )

    async def get_collection(self, collection_id: str | uuid.UUID) -> dict:
        async with db.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM collections WHERE id=%s",
                (str(collection_id),),
            )
            return await cur.fetchone()

    async def replace_chunks(self, doc: dict, drafts: list, config: dict) -> dict:
        doc_id = str(doc["id"])
        async with db.pool.connection() as conn:
            async with conn.transaction():
                for draft in drafts:
                    await conn.execute(
                        """INSERT INTO chunks(id, document_id, collection_id, seq, content, loc)
                           VALUES (%s,%s,%s,%s,%s,%s::jsonb)
                           ON CONFLICT (document_id, seq)
                           DO UPDATE SET content=EXCLUDED.content, loc=EXCLUDED.loc""",
                        (
                            chunk_uuid(doc_id, draft.seq),
                            doc_id,
                            str(doc["collection_id"]),
                            draft.seq,
                            draft.text,
                            json.dumps(draft.loc, ensure_ascii=False),
                        ),
                    )

                await conn.execute(
                    "DELETE FROM chunks WHERE document_id=%s AND seq >= %s",
                    (doc_id, len(drafts)),
                )

                cur = await conn.execute(
                    """SELECT count(*) AS total,
                              count(*) FILTER (WHERE embedded) AS done
                       FROM chunks WHERE document_id=%s""",
                    (doc_id,),
                )
                counts = await cur.fetchone()

                await conn.execute(
                    """UPDATE documents SET chunks_total=%s, chunks_embedded=%s,
                           ingest_config=%s::jsonb, updated_at=now() WHERE id=%s""",
                    (
                        counts["total"],
                        counts["done"],
                        json.dumps(config, ensure_ascii=False),
                        doc_id,
                    ),
                )
        return counts

    async def next_embedding_batch(self, doc_id: str, batch_size: int) -> list[dict]:
        async with db.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT id, content FROM chunks
                   WHERE document_id=%s AND embedded=FALSE
                   ORDER BY seq LIMIT %s""",
                (doc_id, batch_size),
            )
            return await cur.fetchall()

    async def persist_embeddings(
        self,
        doc: dict,
        collection: dict,
        model: str,
        dim: int,
        table: str,
        coltype: str,
        batch: list[dict],
        vectors: list[list[float]],
    ) -> None:
        doc_id = str(doc["id"])
        collection_id = str(doc["collection_id"])

        async with db.pool.connection() as conn:
            async with conn.transaction():
                if not collection["embedding_model"]:
                    await conn.execute(
                        """UPDATE collections SET embedding_model=%s, embedding_dim=%s,
                               updated_at=now() WHERE id=%s""",
                        (model, dim, collection_id),
                    )
                    collection["embedding_model"] = model
                    collection["embedding_dim"] = dim
                elif not collection["embedding_dim"]:
                    await conn.execute(
                        "UPDATE collections SET embedding_dim=%s WHERE id=%s",
                        (dim, collection_id),
                    )
                    collection["embedding_dim"] = dim

                for chunk, vector in zip(batch, vectors):
                    await conn.execute(
                        f"""INSERT INTO {table}(chunk_id, embedding)
                            VALUES (%s, %s::{coltype})
                            ON CONFLICT (chunk_id)
                            DO UPDATE SET embedding=EXCLUDED.embedding""",
                        (str(chunk["id"]), vector_literal(vector)),
                    )

                chunk_ids = [str(chunk["id"]) for chunk in batch]
                await conn.execute(
                    "UPDATE chunks SET embedded=TRUE WHERE id = ANY(%s::uuid[])",
                    (chunk_ids,),
                )
                await conn.execute(
                    """UPDATE documents SET chunks_embedded=
                           (SELECT count(*) FROM chunks
                            WHERE document_id=%s AND embedded=TRUE),
                       updated_at=now() WHERE id=%s""",
                    (doc_id, doc_id),
                )

    async def chunks_total(self, doc_id: str) -> int:
        async with db.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT chunks_total FROM documents WHERE id=%s",
                (doc_id,),
            )
            row = await cur.fetchone()
            return row["chunks_total"]


    async def status(self, doc_id: str) -> str | None:
        async with db.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT status FROM documents WHERE id=%s",
                (doc_id,),
            )
            row = await cur.fetchone()
            return row["status"] if row else None

    async def finalize_cancel(self, doc_id: str) -> None:
        async with db.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM chunks WHERE document_id=%s",
                    (doc_id,),
                )
                await conn.execute(
                    """UPDATE documents
                       SET status='cancelled', error=NULL, retry_at=NULL,
                           chunks_total=0, chunks_embedded=0, updated_at=now()
                       WHERE id=%s""",
                    (doc_id,),
                )

class DocumentIngestor:
    """Executa o pipeline de um único documento."""

    def __init__(self, store: IngestStore, stop_event: asyncio.Event) -> None:
        self.store = store
        self.stop_event = stop_event
        self._vector_tables: dict[int, tuple[str, str]] = {}

    async def process(self, doc: dict) -> None:
        doc_id = str(doc["id"])
        try:
            await self._cancel_if_requested(doc)
            await self.store.increment_attempts(doc_id)
            config, model = await self._resolve_config_and_model(doc)
            collection = await self._validated_collection(doc, model)

            if doc["chunks_total"] == 0:
                extracted = await self._extract(doc)
                await self._cancel_if_requested(doc)
                await self._quality_gate(doc, extracted)
                await self._cancel_if_requested(doc)
                await self._chunk(doc, config, extracted)
                await self._cancel_if_requested(doc)

            await self._embed(doc, config, model, collection)
        except DocumentCancelled:
            log.info("doc %s cancelado cooperativamente", doc_id)
        except QualityGateError as exc:
            await self._fail(doc, f"Qualidade: {exc}")
        except ExtractionError as exc:
            await self._fail(doc, f"Extração: {exc}")
        except LMStudioUnavailable as exc:
            await self._fail(doc, f"LM Studio indisponível: {exc}", waiting=True)
        except LMStudioError as exc:
            await self._fail(doc, f"LM Studio: {exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("falha inesperada no doc %s", doc_id)
            await self._fail(
                doc,
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}",
            )

    async def _cancel_if_requested(self, doc: dict) -> None:
        doc_id = str(doc["id"])
        status = await self.store.status(doc_id)
        if status == "cancelled":
            raise DocumentCancelled()
        if status == "cancelling":
            await self.store.finalize_cancel(doc_id)
            extraction_cache_path(doc_id).unlink(missing_ok=True)
            effective = await settings.effective()
            await db.event(
                f"[cancelled] {doc['filename']}",
                "info",
                doc["collection_id"],
                doc_id,
                keep=effective["worker"]["event_log_keep"],
            )
            raise DocumentCancelled()

    async def _resolve_config_and_model(self, doc: dict) -> tuple[dict, str]:
        config = doc["ingest_config"] or await resolve_ingest_config(None, None)
        model = config.get("embedding_model") or await settings.get("embedding_model")
        if not model:
            raise LMStudioError(
                "Nenhum modelo de embedding selecionado. "
                "Configure em Configurações ou no início da ingestão."
            )
        config["embedding_model"] = model
        return config, model

    async def _validated_collection(self, doc: dict, model: str) -> dict:
        collection = await self.store.get_collection(doc["collection_id"])
        existing_model = collection["embedding_model"]
        if existing_model and existing_model != model:
            raise LMStudioError(
                f"Coleção '{collection['name']}' usa o modelo '{existing_model}'. "
                "Reindexe a coleção para trocar de modelo de embedding."
            )
        return collection

    async def _fail(self, doc: dict, message: str, *, waiting: bool = False) -> None:
        effective = await settings.effective()
        ingest_settings = effective["ingest"]

        if waiting:
            delay = min(
                ingest_settings["retry_backoff_base"] * (2 ** min(doc["attempts"], 8)),
                ingest_settings["retry_backoff_max"],
            )
            await self.store.set_status(
                str(doc["id"]),
                "waiting",
                error=message,
                retry_in=delay,
            )
            event_message = (
                f"[waiting] {doc['filename']}: {message} "
                f"(nova tentativa em {delay:.0f}s)"
            )
            level = "warning"
            log.warning(
                "doc %s waiting: %s (retry em %.0fs)",
                doc["id"],
                message,
                delay,
            )
        else:
            await self.store.set_status(str(doc["id"]), "error", error=message)
            event_message = f"[erro] {doc['filename']}: {message}"
            level = "error"
            log.error("doc %s error: %s", doc["id"], message)

        await db.event(
            event_message,
            level,
            doc["collection_id"],
            doc["id"],
            keep=effective["worker"]["event_log_keep"],
        )

    async def _extract(self, doc: dict) -> ExtractedDoc:
        doc_id = str(doc["id"])
        await self.store.set_status(doc_id, "extracting")

        cached = self._read_extraction_cache(doc)
        if cached is not None:
            log.info("doc %s: extração em cache", doc_id)
            return cached

        effective = await settings.effective()
        blob = env.data_dir / doc["stored_path"]
        extracted = await asyncio.to_thread(
            extract,
            blob,
            doc["filename"],
            strip_whitespace=effective["ingest"]["strip_whitespace"],
        )

        await self._cancel_if_requested(doc)
        self._write_extraction_cache(doc, extracted)
        metadata = {**(doc["meta"] or {}), **extracted.meta}
        await self.store.set_status(doc_id, "extracting", meta=metadata)
        return extracted

    def _read_extraction_cache(self, doc: dict) -> ExtractedDoc | None:
        cache = extraction_cache_path(str(doc["id"]))
        if not cache.exists():
            return None

        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("sha256") == doc["content_sha256"]:
                return ExtractedDoc.from_json(data)
        except Exception:
            log.warning("cache de extração inválido para %s; reextraindo", doc["id"])
        return None

    def _write_extraction_cache(self, doc: dict, extracted: ExtractedDoc) -> None:
        cache = extraction_cache_path(str(doc["id"]))
        cache.parent.mkdir(parents=True, exist_ok=True)
        payload = {"sha256": doc["content_sha256"], **extracted.to_json()}
        cache.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

    async def _quality_gate(self, doc: dict, extracted: ExtractedDoc) -> None:
        effective = await settings.effective()
        quality_cfg = effective.get("quality", {})
        if not quality_cfg.get("enabled", True):
            return

        report = analyze_document_quality(extracted, quality_cfg)
        metadata = {**(doc["meta"] or {}), **extracted.meta, "quality": report.to_dict()}
        await self.store.set_status(str(doc["id"]), "extracting", meta=metadata)

        if report.status == "bad" and quality_cfg.get("fail_on_bad", True):
            reasons = "; ".join(report.reasons) or "conteúdo extraído reprovado"
            raise QualityGateError(reasons)

    async def _chunk(self, doc: dict, config: dict, extracted: ExtractedDoc) -> None:
        doc_id = str(doc["id"])
        await self.store.set_status(doc_id, "chunking")

        drafts = await asyncio.to_thread(
            chunk_segments,
            extracted.segments,
            config,
        )
        await self._cancel_if_requested(doc)
        if not drafts:
            raise ExtractionError("chunking não produziu nenhum trecho")

        counts = await self.store.replace_chunks(doc, drafts, config)
        log.info(
            "doc %s: %d chunks (%d já embedados)",
            doc_id,
            counts["total"],
            counts["done"],
        )

    async def _embed(
        self,
        doc: dict,
        config: dict,
        model: str,
        collection: dict,
    ) -> None:
        doc_id = str(doc["id"])
        await self._cancel_if_requested(doc)
        await self.store.set_status(doc_id, "embedding")

        effective = await settings.effective()
        batch_size = int(
            config.get("embed_batch_size")
            or effective["ingest"]["embed_batch_size"]
        )
        client = await settings.lmstudio_client()
        rerank_cfg = effective.get("rerank", {})
        context_length = int(rerank_cfg.get("context_length", 8192))
        requirements = [ModelRequirement(model, context_length)]
        if rerank_cfg.get("enabled") and rerank_cfg.get("mode") == "resident":
            reranker_model = await settings.get("reranker_model", rerank_cfg.get("model"))
            if reranker_model and reranker_model != model:
                requirements.append(ModelRequirement(reranker_model, context_length))

        while not await self._should_pause():
            await self._cancel_if_requested(doc)
            batch = await self.store.next_embedding_batch(doc_id, batch_size)
            if not batch:
                await self._mark_ready(doc, model, effective)
                return

            texts = [chunk["content"] for chunk in batch]
            vectors = await runtime_scheduler.run(
                f"ingest-embed:{doc_id}",
                client,
                requirements,
                lambda: client.embed(texts, model),
            )
            await self._cancel_if_requested(doc)
            dim = len(vectors[0])
            table, coltype = await self._vector_table(dim)
            self._validate_embedding_dimension(collection, dim)

            await self.store.persist_embeddings(
                doc,
                collection,
                model,
                dim,
                table,
                coltype,
                batch,
                vectors,
            )

        log.info("doc %s: pausado no meio dos embeddings (progresso salvo)", doc_id)

    async def _should_pause(self) -> bool:
        return self.stop_event.is_set() or await settings.workers_paused()

    async def _vector_table(self, dim: int) -> tuple[str, str]:
        if dim not in self._vector_tables:
            self._vector_tables[dim] = await db.ensure_vector_table(dim)
        return self._vector_tables[dim]

    @staticmethod
    def _validate_embedding_dimension(collection: dict, dim: int) -> None:
        expected = collection["embedding_dim"]
        if expected and expected != dim:
            raise LMStudioError(
                f"Modelo retornou dimensão {dim}, mas a coleção usa {expected}. "
                "Reindexe a coleção."
            )

    async def _mark_ready(self, doc: dict, model: str, effective: dict) -> None:
        doc_id = str(doc["id"])
        total = await self.store.chunks_total(doc_id)

        await self.store.set_status(
            doc_id,
            "ready",
            processed_at=datetime.now(timezone.utc),
            attempts=0,
            chunks_embedded=total,
        )
        try:
            await rebuild_document_mentions(doc_id)
        except Exception:
            log.exception("doc %s: graph enrichment failed; background worker will retry", doc_id)
        else:
            async with db.pool.connection() as conn:
                await conn.execute(
                    "UPDATE documents SET graph_indexed_at=now() WHERE id=%s",
                    (doc_id,),
                )
        await db.event(
            f"[ok] {doc['filename']} indexado ({total} chunks, modelo {model})",
            "info",
            doc["collection_id"],
            doc_id,
            keep=effective["worker"]["event_log_keep"],
        )
        log.info("doc %s pronto: %d chunks", doc_id, total)


class IngestWorker:
    """Coordena o loop de trabalho e delega o processamento de cada documento."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.current_document: str | None = None
        self._store = IngestStore()
        self._ingestor = DocumentIngestor(self._store, self._stop)

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="ingest-worker")
        log.info("ingest worker started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        log.info("ingest worker stopped")

    async def _loop(self) -> None:
        effective = await settings.effective()
        poll_interval = effective["worker"]["poll_interval"]

        while not self._stop.is_set():
            try:
                if await settings.workers_paused():
                    await asyncio.sleep(poll_interval)
                    continue

                doc = await self._store.claim_next()
                if doc is None:
                    await asyncio.sleep(poll_interval)
                    continue

                await self._process_document(doc)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("erro inesperado no loop do worker")
                await asyncio.sleep(poll_interval)

    async def _process_document(self, doc: dict) -> None:
        self.current_document = str(doc["id"])
        try:
            await self._ingestor.process(doc)
        finally:
            self.current_document = None


worker = IngestWorker()
