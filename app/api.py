"""API REST da interface administrativa (toda administração passa por aqui;
o MCP expõe apenas consulta/leitura)."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from . import autoconfig
from .chunking import ConfigError, validate_ingest_config
from .classification_runtime import classification_worker, classify_ready_document
from .config import env
from .db import db
from .extraction import ExtractionError, extract
from .graph_retrieval import graph_worker, persistent_evidence_graph
from .ingest import ACTIVE_STATUSES, extraction_cache_path, resolve_ingest_config, worker
from .ingest_policy import should_auto_start_upload, should_ignore_empty_upload
from .library_taxonomy import CORE_COLLECTIONS, COMPUTER_BOOKS, computer_books_collection
from .lmstudio import LMStudioClient, LMStudioError, LMStudioUnavailable
from .retrieval_observability import retrieval_metrics
from .search import SearchError, semantic_search
from .search_filters import SearchFilters
from .model_runtime import runtime_scheduler
from .settings import settings
from .source_sync import source_worker, sync_collection_source, validate_interval

log = logging.getLogger("rag.api")
router = APIRouter(prefix="/api")


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(400, f"id inválido: {value}")


# ----------------------------------------------------------------- health
@router.get("/health")
async def health():
    try:
        async with db.pool.connection() as conn:
            cur = await conn.execute("SELECT 1 AS ok")
            await cur.fetchone()
        db_ok = True
    except Exception as e:
        db_ok = False
        log.error("health: db indisponível: %s", e)
    client = await settings.lmstudio_client(timeout=8, retries=1)
    lm = await client.health()
    return {
        "status": "ok" if db_ok else "degraded",
        "database": db_ok,
        "worker": {"running": worker._task is not None and not worker._task.done(),
                   "paused": await settings.paused(),
                   "current_document": worker.current_document},
        "classification_worker": {
            "running": classification_worker._task is not None and not classification_worker._task.done(),
            "current_document": classification_worker.current_document,
        },
        "source_worker": {
            "running": source_worker._task is not None and not source_worker._task.done(),
            "current_source": source_worker.current_source,
        },
        "graph_worker": {
            "running": graph_worker._task is not None and not graph_worker._task.done(),
            "current_document": graph_worker.current_document,
            "indexed_documents": graph_worker.indexed_documents,
            "last_error": graph_worker.last_error,
        },
        "model_runtime": runtime_scheduler.status(),
        "lmstudio": lm,
        "mcp_enabled": env.mcp_enabled,
        "build": {
            "version": env.build_version,
            "commit": env.build_commit,
        },
    }


# ----------------------------------------------------------------- settings
class SettingsPatch(BaseModel):
    lmstudio_url: str | None = None
    embedding_model: str | None = None
    llm_model: str | None = None
    reranker_model: str | None = None
    overrides: dict[str, Any] | None = None


@router.get("/settings")
async def get_settings():
    return {"settings": await settings.raw(),
            "effective": await settings.effective(),
            "paused": await settings.paused()}


@router.put("/settings")
async def put_settings(patch: SettingsPatch):
    try:
        raw = await settings.update(patch.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"settings": raw, "effective": await settings.effective()}


@router.get("/lmstudio/models")
async def lmstudio_models():
    client = await settings.lmstudio_client(timeout=15, retries=1)
    try:
        models = await client.list_models()
    except LMStudioError as e:
        raise HTTPException(503, f"LM Studio indisponível: {e}")
    return {"url": client.base_url, "models": models,
            "selected": {"embedding_model": await settings.get("embedding_model"),
                         "llm_model": await settings.get("llm_model"),
                         "reranker_model": await settings.get("reranker_model")}}


class LMTest(BaseModel):
    url: str | None = None


@router.post("/lmstudio/test")
async def lmstudio_test(body: LMTest):
    url = (body.url or await settings.get("lmstudio_url", "")).rstrip("/")
    client = LMStudioClient(url, timeout=10, max_retries=1)
    result = await client.health()
    if result["reachable"]:
        try:
            result["models_list"] = await client.list_models()
            result["models"] = len(result["models_list"])
        except LMStudioError:
            result["models_list"] = []
            result["models"] = 0
    return result


# ----------------------------------------------------------------- collections
class CollectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    ingest_config: dict[str, Any] | None = None


class CollectionPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    ingest_config: dict[str, Any] | None = None
    default_search: bool | None = None


@router.get("/collections")
async def list_collections():
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"""SELECT c.*,
                (SELECT count(*) FROM documents d WHERE d.collection_id=c.id) AS doc_count,
                (SELECT count(*) FROM documents d WHERE d.collection_id=c.id AND d.status='ready') AS ready_count,
                (SELECT count(*) FROM documents d WHERE d.collection_id=c.id AND d.status='error') AS error_count,
                (SELECT count(*) FROM documents d WHERE d.collection_id=c.id AND d.status='staged') AS staged_count,
                (SELECT count(*) FROM documents d WHERE d.collection_id=c.id AND d.status IN {ACTIVE_STATUSES}) AS active_count,
                (SELECT coalesce(sum(d.chunks_total),0) FROM documents d WHERE d.collection_id=c.id AND d.status='ready') AS chunk_count
                FROM collections c ORDER BY c.name""")
        rows = await cur.fetchall()
    return {"collections": rows}



class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""


class CategoryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None


@router.get("/categories")
async def list_categories(collection_id: str | None = None):
    params: list[Any] = []
    where = ""
    if collection_id:
        where = "WHERE cat.collection_id=%s"
        params.append(_uuid(collection_id))
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"""SELECT cat.*, c.name AS collection,
                       (SELECT count(*) FROM documents d WHERE d.category_id=cat.id) AS doc_count
                FROM categories cat
                JOIN collections c ON c.id=cat.collection_id
                {where}
                ORDER BY cat.name""",
            params,
        )
        rows = await cur.fetchall()
    return {"categories": rows}


@router.post("/collections/{col_id}/categories", status_code=201)
async def create_category(col_id: str, body: CategoryCreate):
    col_id = _uuid(col_id)
    name = body.name.strip()
    async with db.pool.connection() as conn:
        col = await (
            await conn.execute("SELECT name FROM collections WHERE id=%s", (col_id,))
        ).fetchone()
        if not col:
            raise HTTPException(404, "collection not found")
        if col["name"] != COMPUTER_BOOKS:
            raise HTTPException(409, "categories are supported only inside Computer Books")
        cur = await conn.execute(
            """INSERT INTO categories(id,collection_id,name,description)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (collection_id,name) DO NOTHING RETURNING *""",
            (str(uuid.uuid4()), col_id, name, body.description.strip()),
        )
        row = await cur.fetchone()
    if not row:
        raise HTTPException(409, f"category '{name}' already exists")
    return row


@router.patch("/categories/{category_id}")
async def patch_category(category_id: str, body: CategoryPatch):
    category_id = _uuid(category_id)
    sets = ["updated_at=now()"]
    params: list[Any] = []
    if body.name is not None:
        sets.append("name=%s")
        params.append(body.name.strip())
    if body.description is not None:
        sets.append("description=%s")
        params.append(body.description.strip())
    params.append(category_id)
    async with db.pool.connection() as conn:
        row = await (
            await conn.execute(
                f"UPDATE categories SET {', '.join(sets)} WHERE id=%s RETURNING *", params
            )
        ).fetchone()
    if not row:
        raise HTTPException(404, "category not found")
    return row


@router.delete("/categories/{category_id}")
async def delete_category(category_id: str):
    category_id = _uuid(category_id)
    async with db.pool.connection() as conn:
        row = await (
            await conn.execute("DELETE FROM categories WHERE id=%s RETURNING name", (category_id,))
        ).fetchone()
    if not row:
        raise HTTPException(404, "category not found")
    return {"deleted": row["name"]}


@router.post("/collections", status_code=201)
async def create_collection(body: CollectionCreate):
    raise HTTPException(
        409,
        "top-level collection topology is fixed; create a category inside Computer Books instead",
    )


@router.patch("/collections/{col_id}")
async def patch_collection(col_id: str, body: CollectionPatch):
    col_id = _uuid(col_id)
    async with db.pool.connection() as conn:
        current = await (
            await conn.execute("SELECT name FROM collections WHERE id=%s", (col_id,))
        ).fetchone()
    if not current:
        raise HTTPException(404, "coleção não encontrada")
    if body.name is not None and current["name"] in CORE_COLLECTIONS:
        raise HTTPException(409, "core collections cannot be renamed")
    sets, params = ["updated_at=now()"], []
    if body.name is not None:
        sets.append("name=%s"); params.append(body.name.strip())
    if body.description is not None:
        sets.append("description=%s"); params.append(body.description)
    if body.default_search is not None:
        sets.append("default_search=%s"); params.append(body.default_search)
    if body.ingest_config is not None:
        eff = await settings.effective()
        try:
            cfg = validate_ingest_config({**eff["ingest"], **body.ingest_config}, eff["limits"])
        except ConfigError as e:
            raise HTTPException(400, str(e))
        sets.append("ingest_config=%s::jsonb"); params.append(json.dumps(cfg))
    params.append(col_id)
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"UPDATE collections SET {', '.join(sets)} WHERE id=%s RETURNING *", params)
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "coleção não encontrada")
    return row


@router.delete("/collections/{col_id}")
async def delete_collection(col_id: str):
    col_id = _uuid(col_id)
    async with db.pool.connection() as conn:
        current = await (
            await conn.execute("SELECT name FROM collections WHERE id=%s", (col_id,))
        ).fetchone()
        if not current:
            raise HTTPException(404, "coleção não encontrada")
        if current["name"] in CORE_COLLECTIONS:
            raise HTTPException(409, "core collections cannot be deleted")
        cur = await conn.execute(
            "SELECT stored_path FROM documents WHERE collection_id=%s", (col_id,))
        paths = [r["stored_path"] for r in await cur.fetchall()]
        cur = await conn.execute("DELETE FROM collections WHERE id=%s RETURNING name", (col_id,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "coleção não encontrada")
    await _gc_blobs(paths)
    await db.event(f"Coleção excluída: {row['name']}")
    return {"deleted": row["name"]}


class ReindexAll(BaseModel):
    config: dict[str, Any] | None = None
    embedding_model: str | None = None


@router.post("/collections/{col_id}/reindex")
async def reindex_collection(col_id: str, body: ReindexAll):
    col_id = _uuid(col_id)
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM collections WHERE id=%s", (col_id,))
        col = await cur.fetchone()
    if not col:
        raise HTTPException(404, "coleção não encontrada")
    model = body.embedding_model or col["embedding_model"] or await settings.get("embedding_model")
    if not model:
        raise HTTPException(400, "nenhum modelo de embedding selecionado")
    cfg = await resolve_ingest_config(col["ingest_config"], body.config)
    cfg["embedding_model"] = model
    async with db.pool.connection() as conn:
        async with conn.transaction():
            if model != col["embedding_model"]:
                await conn.execute(
                    """UPDATE collections SET embedding_model=NULL, embedding_dim=NULL,
                           ingest_config=%s::jsonb, updated_at=now() WHERE id=%s""",
                    (json.dumps(cfg), col_id))
            else:
                await conn.execute(
                    "UPDATE collections SET ingest_config=%s::jsonb, updated_at=now() WHERE id=%s",
                    (json.dumps(cfg), col_id))
            await conn.execute("DELETE FROM chunks WHERE collection_id=%s", (col_id,))
            cur = await conn.execute(
                """UPDATE documents SET status='pending', error=NULL, retry_at=NULL,
                       attempts=0, chunks_total=0, chunks_embedded=0,
                       ingest_config=%s::jsonb, updated_at=now()
                   WHERE collection_id=%s RETURNING id""",
                (json.dumps(cfg), col_id))
            n = len(await cur.fetchall())
    await db.event(f"Reindexação da coleção '{col['name']}' iniciada ({n} documentos, modelo {model})",
                   collection_id=col_id)
    return {"reindexing": n, "embedding_model": model, "config": cfg}


# ----------------------------------------------------------------- directory sources
class CollectionSourceCreate(BaseModel):
    path: str = Field(min_length=1)
    interval_seconds: int | None = None
    enabled: bool = True
    exclude_patterns: list[str] = Field(default_factory=list)
    source_type: str = "directory"
    category_id: str | None = None


class CollectionSourcePatch(BaseModel):
    path: str | None = None
    interval_seconds: int | None = None
    enabled: bool | None = None
    exclude_patterns: list[str] | None = None
    collection_id: str | None = None
    category_id: str | None = None
    source_type: str | None = None


@router.get("/collection-sources")
async def list_collection_sources(collection_id: str | None = None):
    where, params = "", []
    if collection_id:
        where = "WHERE s.collection_id=%s"
        params.append(_uuid(collection_id))
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"""SELECT s.*, c.name AS collection, cat.name AS category
                FROM collection_sources s
                JOIN collections c ON c.id=s.collection_id
                LEFT JOIN categories cat ON cat.id=s.category_id
                {where} ORDER BY c.name, s.path""", params
        )
        return {"sources": await cur.fetchall()}


@router.post("/collections/{col_id}/sources", status_code=201)
async def create_collection_source(col_id: str, body: CollectionSourceCreate):
    col_id = _uuid(col_id)
    eff = await settings.effective()
    cfg = eff.get("sources", {})
    interval = body.interval_seconds or int(cfg["default_interval_seconds"])
    try:
        interval = validate_interval(interval, cfg)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if body.source_type not in {"directory", "repository"}:
        raise HTTPException(400, "source_type must be directory or repository")
    path = str(Path(body.path).expanduser().resolve())
    if not Path(path).is_dir():
        raise HTTPException(400, f"directory not found or not readable: {path}")
    category_id = _uuid(body.category_id) if body.category_id else None
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT id FROM collections WHERE id=%s", (col_id,))
        if not await cur.fetchone():
            raise HTTPException(404, "collection not found")
        if category_id:
            category = await (
                await conn.execute(
                    "SELECT id FROM categories WHERE id=%s AND collection_id=%s",
                    (category_id, col_id),
                )
            ).fetchone()
            if not category:
                raise HTTPException(400, "category does not belong to target collection")
        try:
            cur = await conn.execute(
                """INSERT INTO collection_sources(
                       id, collection_id, category_id, path, enabled, interval_seconds,
                       exclude_patterns, next_sync_at, source_type)
                   VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,now(),%s) RETURNING *""",
                (str(uuid.uuid4()), col_id, category_id, path, body.enabled, interval,
                 json.dumps(body.exclude_patterns), body.source_type),
            )
            row = await cur.fetchone()
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                raise HTTPException(409, "source already exists for this collection") from e
            raise
    return row


@router.patch("/collection-sources/{source_id}")
async def patch_collection_source(source_id: str, body: CollectionSourcePatch):
    source_id = _uuid(source_id)
    eff = await settings.effective()
    cfg = eff.get("sources", {})

    async with db.pool.connection() as conn:
        async with conn.transaction():
            current = await (
                await conn.execute(
                    "SELECT * FROM collection_sources WHERE id=%s FOR UPDATE", (source_id,)
                )
            ).fetchone()
            if not current:
                raise HTTPException(404, "source not found")

            target_collection = (
                _uuid(body.collection_id)
                if body.collection_id is not None
                else current["collection_id"]
            )
            collection = await (
                await conn.execute("SELECT id FROM collections WHERE id=%s", (target_collection,))
            ).fetchone()
            if not collection:
                raise HTTPException(404, "target collection not found")

            category_was_set = "category_id" in body.model_fields_set
            if category_was_set:
                target_category = _uuid(body.category_id) if body.category_id else None
            elif target_collection != current["collection_id"]:
                target_category = None
            else:
                target_category = current.get("category_id")

            if target_category:
                category = await (
                    await conn.execute(
                        "SELECT id FROM categories WHERE id=%s AND collection_id=%s",
                        (target_category, target_collection),
                    )
                ).fetchone()
                if not category:
                    raise HTTPException(400, "category does not belong to target collection")

            if body.source_type is not None and body.source_type not in {"directory", "repository"}:
                raise HTTPException(400, "source_type must be directory or repository")

            if target_collection != current["collection_id"]:
                duplicate = await (
                    await conn.execute(
                        """SELECT d.id FROM documents d
                           WHERE d.source_id=%s AND EXISTS (
                             SELECT 1 FROM documents x
                             WHERE x.collection_id=%s
                               AND x.content_sha256=d.content_sha256
                               AND x.id<>d.id
                           ) LIMIT 1""",
                        (source_id, target_collection),
                    )
                ).fetchone()
                if duplicate:
                    raise HTTPException(
                        409,
                        "cannot move source: target collection already contains identical content",
                    )

            sets, params = ["updated_at=now()"], []
            if body.path is not None:
                path = str(Path(body.path).expanduser().resolve())
                if not Path(path).is_dir():
                    raise HTTPException(400, f"directory not found or not readable: {path}")
                sets.extend(["path=%s", "next_sync_at=now()"]); params.append(path)
            if body.interval_seconds is not None:
                try:
                    interval = validate_interval(body.interval_seconds, cfg)
                except ValueError as e:
                    raise HTTPException(400, str(e)) from e
                sets.extend(["interval_seconds=%s", "next_sync_at=now()"]); params.append(interval)
            if body.enabled is not None:
                sets.append("enabled=%s"); params.append(body.enabled)
                if body.enabled:
                    sets.append("next_sync_at=now()")
            if body.exclude_patterns is not None:
                sets.extend(["exclude_patterns=%s::jsonb", "next_sync_at=now()"])
                params.append(json.dumps(body.exclude_patterns))
            if body.source_type is not None:
                sets.extend(["source_type=%s", "next_sync_at=now()"])
                params.append(body.source_type)
            if target_collection != current["collection_id"]:
                sets.extend(["collection_id=%s", "next_sync_at=now()"])
                params.append(target_collection)
            if category_was_set or target_collection != current["collection_id"]:
                sets.extend(["category_id=%s", "next_sync_at=now()"])
                params.append(target_category)

            sets = list(dict.fromkeys(sets))
            params.append(source_id)
            row = await (
                await conn.execute(
                    f"UPDATE collection_sources SET {', '.join(sets)} WHERE id=%s RETURNING *",
                    params,
                )
            ).fetchone()

            if target_collection != current["collection_id"]:
                await conn.execute(
                    """UPDATE documents SET collection_id=%s,category_id=%s,updated_at=now()
                       WHERE source_id=%s""",
                    (target_collection, target_category, source_id),
                )
                await conn.execute(
                    """UPDATE chunks SET collection_id=%s
                       WHERE document_id IN (
                         SELECT id FROM documents WHERE source_id=%s
                       )""",
                    (target_collection, source_id),
                )
            elif category_was_set:
                await conn.execute(
                    "UPDATE documents SET category_id=%s,updated_at=now() WHERE source_id=%s",
                    (target_category, source_id),
                )

    return row


@router.post("/collection-sources/{source_id}/sync")
async def sync_collection_source_route(source_id: str):
    source_id = _uuid(source_id)
    try:
        stats = await sync_collection_source(source_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"source_id": source_id, "stats": stats}


@router.delete("/collection-sources/{source_id}")
async def delete_collection_source(source_id: str):
    source_id = _uuid(source_id)
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM collection_sources WHERE id=%s RETURNING path", (source_id,)
        )
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "source not found")
    return {"deleted": row["path"]}


AUTO_COLLECTION_SENTINEL = "__auto__"


async def _ensure_auto_inbox() -> dict[str, Any]:
    # Automatic book imports are indexed directly into Computer Books.
    # Category remains NULL until the post-ingest classifier assigns one.
    return await computer_books_collection(db, settings)


# ----------------------------------------------------------------- documents
def _safe_relpath(path: str, fallback: str) -> str:
    p = PurePosixPath((path or fallback).replace("\\", "/"))
    parts = [x for x in p.parts if x not in ("..", "/", ".", "")]
    return "/".join(parts) or fallback


async def _gc_blobs(stored_paths: list[str]) -> None:
    if not stored_paths:
        return
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT stored_path FROM documents WHERE stored_path = ANY(%s)",
            (stored_paths,))
        still_used = {r["stored_path"] for r in await cur.fetchall()}
    for p in set(stored_paths) - still_used:
        try:
            (env.data_dir / p).unlink(missing_ok=True)
        except OSError:
            log.warning("não foi possível remover blob %s", p)


@router.get("/documents")
async def list_documents(collection_id: str | None = None,
                         collection_ids: list[str] = Query(default=[]),
                         exclude_collection_ids: list[str] = Query(default=[]),
                         status: str | None = None, q: str | None = None,
                         limit: int = Query(100, le=500), offset: int = 0):
    where, params = [], []
    include_ids = [_uuid(x) for x in collection_ids]
    exclude_ids = [_uuid(x) for x in exclude_collection_ids]
    if collection_id:
        include_ids.append(_uuid(collection_id))
    if include_ids:
        placeholders = ",".join(["%s"] * len(include_ids))
        where.append(f"d.collection_id IN ({placeholders})")
        params.extend(include_ids)
    if exclude_ids:
        placeholders = ",".join(["%s"] * len(exclude_ids))
        where.append(f"d.collection_id NOT IN ({placeholders})")
        params.extend(exclude_ids)
    if status:
        where.append("d.status=%s"); params.append(status)
    if q:
        where.append("(d.filename ILIKE %s OR d.source_path ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"""SELECT d.*, c.name AS collection, cat.name AS category FROM documents d
                JOIN collections c ON c.id=d.collection_id
                LEFT JOIN categories cat ON cat.id=d.category_id {clause}
                ORDER BY d.created_at DESC LIMIT %s OFFSET %s""",
            params + [limit, offset])
        items = await cur.fetchall()
        cur = await conn.execute(
            f"SELECT count(*) AS n FROM documents d {clause}", params)
        total = (await cur.fetchone())["n"]
    return {"items": items, "total": total}


@router.get("/documents/{doc_id}")
async def get_document(doc_id: str):
    doc_id = _uuid(doc_id)
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT d.*, c.name AS collection, cat.name AS category FROM documents d
               JOIN collections c ON c.id=d.collection_id
               LEFT JOIN categories cat ON cat.id=d.category_id WHERE d.id=%s""", (doc_id,))
        doc = await cur.fetchone()
        if not doc:
            raise HTTPException(404, "documento não encontrado")
        cur = await conn.execute(
            """SELECT seq, left(content, 400) AS preview, loc, embedded
               FROM chunks WHERE document_id=%s ORDER BY seq LIMIT 10""", (doc_id,))
        doc["chunk_preview"] = await cur.fetchall()
    return doc


@router.post("/documents/upload", status_code=201)
async def upload_documents(collection_id: str = Form(...),
                           files: list[UploadFile] = File(...),
                           paths: list[str] = Form(default=[])):
    auto_classify = collection_id == AUTO_COLLECTION_SENTINEL
    if auto_classify:
        default_model = await settings.get("embedding_model")
        if not default_model:
            raise HTTPException(400, "automatic classification requires a configured embedding model")
        collection = await _ensure_auto_inbox()
        collection_id = str(collection["id"])
    else:
        collection_id = _uuid(collection_id)
        async with db.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, name, ingest_config, embedding_model FROM collections WHERE id=%s",
                (collection_id,),
            )
            collection = await cur.fetchone()
            if not collection:
                raise HTTPException(404, "collection not found")

    eff = await settings.effective()
    default_model = locals().get("default_model") or await settings.get("embedding_model")
    auto_start = auto_classify or should_auto_start_upload(
        enabled=bool(eff.get("workflow", {}).get("auto_start_configured_uploads", False)),
        collection_config=collection["ingest_config"],
        collection_model=collection["embedding_model"],
        default_model=default_model,
    )
    auto_config = None
    if auto_start:
        auto_config = await resolve_ingest_config(collection["ingest_config"], None)
        auto_config["embedding_model"] = collection["embedding_model"] or default_model

    allowed = {e.lower() for e in eff["allowed_extensions"]}
    max_bytes = int(eff["ingest"]["max_file_mb"]) * 1024 * 1024
    tmp_dir = env.data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for i, up in enumerate(files):
        filename = Path(up.filename or f"arquivo_{i}").name
        rel = _safe_relpath(paths[i] if i < len(paths) else "", filename)
        ext = Path(filename).suffix.lower()
        if ext not in allowed:
            results.append({"filename": filename, "source_path": rel,
                            "status": "unsupported",
                            "error": f"extensão {ext or '(sem extensão)'} não suportada"})
            continue
        tmp = tmp_dir / f"upload_{uuid.uuid4()}"
        sha = hashlib.sha256()
        size = 0
        try:
            with open(tmp, "wb") as f:
                while chunk := await up.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError(f"arquivo excede o limite de {eff['ingest']['max_file_mb']} MB")
                    sha.update(chunk)
                    f.write(chunk)
            if should_ignore_empty_upload(size_bytes=size):
                results.append({"filename": filename, "source_path": rel, "status": "ignored_empty", "size_bytes": size, "error": "arquivo vazio ignorado"})
                continue

            digest = sha.hexdigest()
            blob_rel = f"blobs/{digest[:2]}/{digest}"
            blob = env.data_dir / blob_rel
            blob.parent.mkdir(parents=True, exist_ok=True)
            if not blob.exists():
                shutil.move(str(tmp), blob)
            doc_id = str(uuid.uuid4())
            async with db.pool.connection() as conn:
                initial_status = "pending" if auto_start else "staged"
                cur = await conn.execute(
                    """INSERT INTO documents(id, collection_id, filename, source_path,
                           stored_path, content_sha256, size_bytes, status, ingest_config, meta)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
                       ON CONFLICT (collection_id, content_sha256) DO NOTHING RETURNING id""",
                    (
                        doc_id, collection_id, filename, rel, blob_rel, digest, size,
                        initial_status,
                        json.dumps(auto_config) if auto_config else None,
                        json.dumps({"auto_classify": True}) if auto_classify else "{}",
                    ))
                row = await cur.fetchone()
                if row:
                    results.append({"filename": filename, "source_path": rel,
                                    "status": initial_status, "document_id": doc_id,
                                    "size_bytes": size, "auto_classify": auto_classify})
                else:
                    cur = await conn.execute(
                        """SELECT id, status FROM documents
                           WHERE collection_id=%s AND content_sha256=%s""",
                        (collection_id, digest))
                    existing = await cur.fetchone()
                    results.append({"filename": filename, "source_path": rel,
                                    "status": "duplicate",
                                    "document_id": str(existing["id"]),
                                    "existing_status": existing["status"],
                                    "error": "conteúdo idêntico já importado nesta coleção"})
        except ValueError as e:
            results.append({"filename": filename, "source_path": rel,
                            "status": "too_large", "error": str(e)})
        except Exception as e:
            log.exception("falha no upload de %s", filename)
            results.append({"filename": filename, "source_path": rel,
                            "status": "error", "error": str(e)})
        finally:
            tmp.unlink(missing_ok=True)
            await up.close()
    staged = sum(1 for r in results if r["status"] == "staged")
    queued = sum(1 for r in results if r["status"] == "pending")
    if queued:
        if auto_classify:
            event_message = (
                f"{queued} document(s) uploaded to {collection['name']} and queued for ingestion; "
                "automatic classification will run after indexing"
            )
        else:
            event_message = (
                f"{queued} document(s) uploaded to {collection['name']} and queued for ingestion "
                "(manual destination; automatic classification not requested)"
            )
        await db.event(event_message, collection_id=collection_id)
    if staged:
        message = (
            f"{staged} document(s) awaiting ingestion configuration"
            if auto_classify else
            f"{staged} document(s) imported, awaiting ingestion configuration"
        )
        await db.event(message, collection_id=collection_id)
    return {
        "results": results,
        "staged": staged,
        "queued": queued,
        "duplicates": sum(1 for r in results if r["status"] == "duplicate"),
        "ignored_empty": sum(1 for r in results if r["status"] == "ignored_empty"),
        "auto_classifying": sum(1 for r in results if r.get("auto_classify")),
        "route_mode": "automatic_classification" if auto_classify else "manual_destination",
        "destination_collection": collection["name"],
    }


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: str):
    doc_id = _uuid(doc_id)
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM documents WHERE id=%s RETURNING filename, stored_path, collection_id",
            (doc_id,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "documento não encontrado")
    await _gc_blobs([row["stored_path"]])
    extraction_cache_path(doc_id).unlink(missing_ok=True)
    await db.event(f"Documento excluído: {row['filename']}",
                   collection_id=row["collection_id"])
    return {"deleted": row["filename"]}


@router.post("/documents/{doc_id}/cancel")
async def cancel_document(doc_id: str):
    doc_id = _uuid(doc_id)
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT id, filename, status, collection_id FROM documents WHERE id=%s FOR UPDATE",
                (doc_id,),
            )
            doc = await cur.fetchone()
            if not doc:
                raise HTTPException(404, "documento não encontrado")

            if doc["status"] == "cancelled":
                return {
                    "cancelled": doc["filename"],
                    "status": "cancelled",
                    "unchanged": True,
                }

            if doc["status"] in ("pending", "waiting", "staged", "error"):
                await conn.execute("DELETE FROM chunks WHERE document_id=%s", (doc_id,))
                await conn.execute(
                    """UPDATE documents
                       SET status='cancelled', error=NULL, retry_at=NULL,
                           chunks_total=0, chunks_embedded=0, updated_at=now()
                       WHERE id=%s""",
                    (doc_id,),
                )
                status = "cancelled"
            elif doc["status"] in ("extracting", "chunking", "embedding", "cancelling"):
                await conn.execute(
                    "UPDATE documents SET status='cancelling', retry_at=NULL, updated_at=now() WHERE id=%s",
                    (doc_id,),
                )
                status = "cancelling"
            else:
                raise HTTPException(
                    409,
                    f"documento não pode ser cancelado no estado '{doc['status']}'",
                )

    if status == "cancelled":
        extraction_cache_path(doc_id).unlink(missing_ok=True)
    await db.event(
        f"Cancelamento solicitado: {doc['filename']} ({status})",
        collection_id=doc["collection_id"],
        document_id=doc_id,
    )
    return {
        "cancelled": doc["filename"],
        "status": status,
        "unchanged": False,
    }


@router.post("/documents/{doc_id}/retry")
async def retry_document(doc_id: str):
    doc_id = _uuid(doc_id)
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """UPDATE documents SET status='pending', error=NULL, retry_at=NULL,
                   updated_at=now() WHERE id=%s AND status IN ('error','waiting')
               RETURNING filename""", (doc_id,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "documento não encontrado ou não está em erro/espera")
    return {"retrying": row["filename"]}


class ClassifyDocumentRequest(BaseModel):
    model: str | None = None


@router.post("/documents/{doc_id}/classify")
async def classify_document_route(doc_id: str, body: ClassifyDocumentRequest):
    doc_id = _uuid(doc_id)
    model = body.model or await settings.get("embedding_model")
    if not model:
        raise HTTPException(400, "select an embedding model before classifying")
    try:
        return await classify_ready_document(doc_id, model)
    except ExtractionError as e:
        raise HTTPException(422, f"could not extract document sample: {e}") from e
    except LMStudioError as e:
        raise HTTPException(503, f"classification models unavailable: {e}") from e
    except ValueError as e:
        raise HTTPException(502, f"invalid classifier response: {e}") from e




class BulkClassifyRequest(BaseModel):
    document_ids: list[str] = Field(default_factory=list, max_length=500)
    all_filtered: bool = False
    collection_id: str | None = None
    q: str | None = None


@router.post("/documents/classify-bulk")
async def classify_documents_bulk(body: BulkClassifyRequest):
    if not await settings.get("embedding_model"):
        raise HTTPException(400, "select an embedding model before classifying")
    if not body.document_ids and not body.all_filtered:
        raise HTTPException(400, "select document_ids or set all_filtered=true")

    where = ["d.status='ready'"]
    params: list[Any] = []
    if body.document_ids:
        ids = [_uuid(x) for x in body.document_ids]
        placeholders = ",".join(["%s"] * len(ids))
        where.append(f"d.id IN ({placeholders})")
        params.extend(ids)
    elif body.all_filtered:
        if body.collection_id:
            where.append("d.collection_id=%s")
            params.append(_uuid(body.collection_id))
        if body.q:
            where.append("(d.filename ILIKE %s OR d.source_path ILIKE %s)")
            params.extend([f"%{body.q}%", f"%{body.q}%"])

    clause = " AND ".join(where)
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                f"SELECT d.id,d.filename FROM documents d WHERE {clause} ORDER BY d.created_at",
                params,
            )
            docs = await cur.fetchall()
            if not docs:
                return {"queued": 0, "document_ids": []}
            ids = [str(d["id"]) for d in docs]
            placeholders = ",".join(["%s"] * len(ids))
            await conn.execute(
                f"""UPDATE documents SET
                       auto_suggestion=NULL,
                       meta=((coalesce(meta,'{{}}'::jsonb)
                              - 'auto_classify_attempts' - 'auto_classify_next_at')
                             || '{{"auto_classify":true}}'::jsonb),
                       updated_at=now()
                   WHERE id IN ({placeholders})""",
                ids,
            )
    await db.event(
        f"Bulk automatic classification queued: {len(ids)} indexed document(s)",
    )
    return {"queued": len(ids), "document_ids": ids}





class DocMove(BaseModel):
    target_collection_id: str


@router.post("/documents/{doc_id}/move")
async def move_document(doc_id: str, body: DocMove):
    doc_id = _uuid(doc_id)
    target_collection_id = _uuid(body.target_collection_id)

    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT d.id, d.filename, d.status, d.content_sha256, d.collection_id,
                      src.name AS source_collection,
                      src.embedding_model AS source_model,
                      src.embedding_dim AS source_dim
               FROM documents d
               JOIN collections src ON src.id=d.collection_id
               WHERE d.id=%s""",
            (doc_id,),
        )
        doc = await cur.fetchone()
        if not doc:
            raise HTTPException(404, "documento não encontrado")

        if doc["status"] in ACTIVE_STATUSES:
            raise HTTPException(409, "documento está em processamento; mova após a ingestão terminar")

        cur = await conn.execute(
            """SELECT id, name, embedding_model, embedding_dim
               FROM collections WHERE id=%s""",
            (target_collection_id,),
        )
        target = await cur.fetchone()
        if not target:
            raise HTTPException(404, "coleção destino não encontrada")

        if str(doc["collection_id"]) == target_collection_id:
            return {
                "moved": doc["filename"],
                "from_collection": doc["source_collection"],
                "to_collection": target["name"],
                "unchanged": True,
            }

        if doc["status"] == "ready":
            if doc["source_model"] != target["embedding_model"] or doc["source_dim"] != target["embedding_dim"]:
                raise HTTPException(
                    409,
                    "coleções usam embeddings incompatíveis; reindexe o documento na coleção destino",
                )

        cur = await conn.execute(
            """SELECT id FROM documents
               WHERE collection_id=%s AND content_sha256=%s AND id<>%s""",
            (target_collection_id, doc["content_sha256"], doc_id),
        )
        if await cur.fetchone():
            raise HTTPException(409, "conteúdo idêntico já existe na coleção destino")

        async with conn.transaction():
            await conn.execute(
                "UPDATE documents SET collection_id=%s, updated_at=now() WHERE id=%s",
                (target_collection_id, doc_id),
            )
            await conn.execute(
                "UPDATE chunks SET collection_id=%s WHERE document_id=%s",
                (target_collection_id, doc_id),
            )

    await db.event(
        f"Documento movido: {doc['filename']} ({doc['source_collection']} → {target['name']})",
        collection_id=target_collection_id,
        document_id=doc_id,
    )
    return {
        "moved": doc["filename"],
        "from_collection": doc["source_collection"],
        "to_collection": target["name"],
        "unchanged": False,
    }


class DocReindex(BaseModel):
    config: dict[str, Any] | None = None
    embedding_model: str | None = None


@router.post("/documents/{doc_id}/reindex")
async def reindex_document(doc_id: str, body: DocReindex):
    doc_id = _uuid(doc_id)
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT d.*, c.embedding_model AS col_model, c.ingest_config AS col_cfg
               FROM documents d JOIN collections c ON c.id=d.collection_id WHERE d.id=%s""",
            (doc_id,))
        doc = await cur.fetchone()
    if not doc:
        raise HTTPException(404, "documento não encontrado")
    model = (body.embedding_model or (doc["ingest_config"] or {}).get("embedding_model")
             or doc["col_model"] or await settings.get("embedding_model"))
    if not model:
        raise HTTPException(400, "nenhum modelo de embedding selecionado")
    if doc["col_model"] and model != doc["col_model"]:
        raise HTTPException(409, f"coleção usa '{doc['col_model']}'; para trocar o modelo, "
                                 f"reindexe a coleção inteira")
    cfg = await resolve_ingest_config(doc["col_cfg"], body.config)
    cfg["embedding_model"] = model
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT status FROM documents WHERE id=%s FOR UPDATE", (doc_id,))
            current = await cur.fetchone()
            if not current:
                raise HTTPException(404, "documento não encontrado")
            if current["status"] in ACTIVE_STATUSES:
                raise HTTPException(409, "documento está em processamento; aguarde antes de reindexar")
            extraction_cache_path(doc_id).unlink(missing_ok=True)
            await conn.execute("DELETE FROM chunks WHERE document_id=%s", (doc_id,))
            await conn.execute(
                """UPDATE documents SET status='pending', error=NULL, retry_at=NULL,
                       attempts=0, chunks_total=0, chunks_embedded=0,
                       ingest_config=%s::jsonb, updated_at=now() WHERE id=%s""",
                (json.dumps(cfg), doc_id))
    await db.event(f"Reindexação iniciada: {doc['filename']}",
                   collection_id=doc["collection_id"], document_id=doc_id)
    return {"reindexing": doc["filename"], "config": cfg}


# ----------------------------------------------------------------- ingest
class SuggestRequest(BaseModel):
    collection_id: str
    document_ids: list[str] | None = None
    model: str | None = None


@router.post("/ingest/suggest")
async def suggest_ingest(body: SuggestRequest):
    col_id = _uuid(body.collection_id)
    model = body.model or await settings.get("llm_model")
    if not model:
        raise HTTPException(400, "selecione um modelo LLM para o modo automático")
    eff = await settings.effective()
    async with db.pool.connection() as conn:
        if body.document_ids:
            ids = [_uuid(d) for d in body.document_ids]
            cur = await conn.execute(
                "SELECT * FROM documents WHERE id = ANY(%s::uuid[]) AND collection_id=%s",
                (ids, col_id))
        else:
            cur = await conn.execute(
                """SELECT * FROM documents WHERE collection_id=%s AND status='staged'
                   ORDER BY created_at LIMIT %s""",
                (col_id, eff["auto"]["sample_docs"]))
        docs = await cur.fetchall()
    if not docs:
        raise HTTPException(400, "nenhum documento para analisar (importe arquivos primeiro)")
    samples = []
    for d in docs[:eff["auto"]["sample_docs"]]:
        try:
            extracted = await _sample_extract(d)
            text = extracted.full_text()[:eff["auto"]["sample_chars"] * 2]
            samples.append({"filename": d["filename"], "meta": extracted.meta,
                            "text": text, "size_bytes": d["size_bytes"]})
        except ExtractionError as e:
            log.warning("amostra de %s falhou: %s", d["filename"], e)
    if not samples:
        raise HTTPException(400, "não foi possível extrair amostra de nenhum documento")
    client = await settings.lmstudio_client(timeout=eff["auto"]["request_timeout"], retries=1)
    suggestion = await autoconfig.suggest_config(client, model, samples, eff, eff["auto"])
    return suggestion


async def _sample_extract(doc: dict):
    cache = extraction_cache_path(str(doc["id"]))
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("sha256") == doc["content_sha256"]:
                from .extraction import ExtractedDoc
                return ExtractedDoc.from_json(data)
        except Exception:
            pass
    import asyncio
    return await asyncio.to_thread(
        extract, env.data_dir / doc["stored_path"], doc["filename"], sample=True)


class IngestStart(BaseModel):
    collection_id: str | None = None
    document_ids: list[str] | None = None
    config: dict[str, Any] | None = None
    embedding_model: str | None = None
    mode: str = "manual"                     # manual | auto (informativo)
    suggestion: dict[str, Any] | None = None  # resposta de /ingest/suggest, se aceita
    save_as_collection_default: bool = False


@router.post("/ingest/start")
async def start_ingest(body: IngestStart):
    if not body.collection_id and not body.document_ids:
        raise HTTPException(400, "informe collection_id ou document_ids")
    model = body.embedding_model or await settings.get("embedding_model")
    if not model:
        raise HTTPException(400, "selecione um modelo de embedding antes de iniciar")
    async with db.pool.connection() as conn:
        if body.document_ids:
            ids = [_uuid(d) for d in body.document_ids]
            cur = await conn.execute(
                """SELECT d.*, c.embedding_model AS col_model, c.ingest_config AS col_cfg,
                          c.name AS col_name
                   FROM documents d JOIN collections c ON c.id=d.collection_id
                   WHERE d.id = ANY(%s::uuid[])""", (ids,))
        else:
            cur = await conn.execute(
                """SELECT d.*, c.embedding_model AS col_model, c.ingest_config AS col_cfg,
                          c.name AS col_name
                   FROM documents d JOIN collections c ON c.id=d.collection_id
                   WHERE d.collection_id=%s AND d.status='staged'""",
                (_uuid(body.collection_id),))
        docs = await cur.fetchall()
    if not docs:
        raise HTTPException(400, "nenhum documento aguardando ingestão")
    for d in docs:
        if d["col_model"] and d["col_model"] != model:
            raise HTTPException(409, f"coleção '{d['col_name']}' já indexada com "
                                     f"'{d['col_model']}'. Use o mesmo modelo ou reindexe a coleção.")
    by_col: dict[str, list[dict]] = {}
    for d in docs:
        by_col.setdefault(str(d["collection_id"]), []).append(d)
    started = 0
    eff = await settings.effective()
    async with db.pool.connection() as conn:
        for col_id, col_docs in by_col.items():
            cfg = await resolve_ingest_config(col_docs[0]["col_cfg"], body.config)
            cfg["embedding_model"] = model
            if body.save_as_collection_default:
                await conn.execute(
                    "UPDATE collections SET ingest_config=%s::jsonb, updated_at=now() WHERE id=%s",
                    (json.dumps(cfg), col_id))
            for d in col_docs:
                await conn.execute(
                    """UPDATE documents SET status='pending', error=NULL, retry_at=NULL,
                           attempts=0, ingest_config=%s::jsonb, auto_suggestion=%s::jsonb,
                           updated_at=now() WHERE id=%s""",
                    (json.dumps(cfg),
                     json.dumps(body.suggestion) if body.suggestion else None,
                     str(d["id"])))
                started += 1
    await db.event(f"Ingestão iniciada: {started} documento(s), modo {body.mode}, "
                   f"modelo {model}, chunk_size={cfg['chunk_size']}, "
                   f"overlap={cfg['chunk_overlap']}, estratégia={cfg['strategy']}")
    if await settings.paused():
        await settings.set_paused(False)
    return {"started": started, "config": cfg}


@router.post("/ingest/pause")
async def pause_ingest():
    await settings.set_paused(True)
    await db.event("Ingestão pausada pelo usuário", "warning")
    return {"paused": True}


@router.post("/ingest/resume")
async def resume_ingest():
    await settings.set_paused(False)
    await db.event("Ingestão retomada pelo usuário")
    return {"paused": False}


@router.post("/runtime/deploy-pause")
async def pause_for_deploy():
    await settings.set_deployment_paused(True)
    await db.event("Background workers paused for deploy validation", "warning")
    return {"deployment_paused": True}


@router.post("/runtime/deploy-resume")
async def resume_after_deploy():
    await settings.set_deployment_paused(False)
    await db.event("Background workers resumed after deploy validation")
    return {"deployment_paused": False}


@router.get("/ingest/status")
async def ingest_status():
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT status, count(*) AS n FROM documents GROUP BY status")
        counts = {r["status"]: r["n"] for r in await cur.fetchall()}
        cur = await conn.execute(
            f"""SELECT coalesce(sum(chunks_total),0) AS total,
                      coalesce(sum(chunks_embedded),0) AS done,
                      count(*) AS docs
               FROM documents WHERE status IN {ACTIVE_STATUSES}""")
        progress = await cur.fetchone()
        cur = await conn.execute(
            """SELECT id, filename, status, error, updated_at FROM documents
               WHERE status IN ('error','waiting') ORDER BY updated_at DESC LIMIT 10""")
        problems = await cur.fetchall()
        current = None
        if worker.current_document:
            cur = await conn.execute(
                """SELECT id, filename, status, chunks_total, chunks_embedded
                   FROM documents WHERE id=%s""", (worker.current_document,))
            current = await cur.fetchone()
    return {
        "paused": await settings.paused(),
        "counts": counts,
        "active_documents": progress["docs"],
        "chunks_total": progress["total"],
        "chunks_embedded": progress["done"],
        "chunks_remaining": progress["total"] - progress["done"],
        "current_document": current,
        "problems": problems,
    }


@router.get("/events")
async def list_events(limit: int = Query(50, le=500)):
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT %s", (limit,))
        return {"events": await cur.fetchall()}


# ----------------------------------------------------------------- search
class SearchRequest(BaseModel):
    query: str
    collections: list[str] | None = None
    document_ids: list[str] | None = None
    authors: list[str] | None = None
    source_paths: list[str] | None = None
    chapters: list[str] | None = None
    pages: list[int] | None = None
    top_k: int | None = None
    min_score: float | None = None
    include_graph: bool = False

    def filters(self) -> SearchFilters:
        return SearchFilters(
            document_ids=self.document_ids or [],
            authors=self.authors or [],
            source_paths=self.source_paths or [],
            chapters=self.chapters or [],
            pages=self.pages or [],
        )


@router.post("/search")
async def search(body: SearchRequest):
    try:
        result = await semantic_search(
            body.query,
            body.collections,
            body.top_k,
            body.min_score,
            body.filters(),
        )
        if body.include_graph:
            cfg = (await settings.effective())["search"]
            result["graph"] = await persistent_evidence_graph(
                result.get("results") or [], body.query, int(cfg["graph_max_edges"])
            )
        return result
    except SearchError as e:
        raise HTTPException(400, str(e))
    except LMStudioUnavailable as e:
        raise HTTPException(503, f"LM Studio indisponível para embedar a consulta: {e}")
    except LMStudioError as e:
        raise HTTPException(502, f"Erro no LM Studio: {e}")


@router.get("/search/metrics")
async def search_metrics(hours: int = Query(24, ge=1, le=2160),
                         recent_limit: int = Query(20, ge=1, le=100)):
    return await retrieval_metrics(hours, recent_limit)
