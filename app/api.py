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
from .collection_classifier import classify_document
from .config import env
from .db import db
from .extraction import ExtractionError, extract
from .ingest import ACTIVE_STATUSES, extraction_cache_path, resolve_ingest_config, worker
from .ingest_policy import should_auto_start_upload, should_ignore_empty_upload
from .lmstudio import LMStudioClient, LMStudioError, LMStudioUnavailable
from .retrieval_observability import retrieval_metrics
from .search import SearchError, semantic_search
from .search_filters import SearchFilters
from .settings import settings

log = logging.getLogger("rag.api")
router = APIRouter(prefix="/api")


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(400, f"id inválido: {value}")


# ----------------------------------------------------------------- health
def _dev_reload_token() -> str:
    roots = (env.defaults_file.parent, Path(__file__).resolve().parent)
    latest = 0
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    latest = max(latest, path.stat().st_mtime_ns)
                except OSError:
                    continue
    return str(latest)


@router.get("/dev/reload")
async def dev_reload_state():
    return {
        "enabled": env.dev_reload,
        "token": _dev_reload_token() if env.dev_reload else None,
        "poll_ms": env.dev_reload_poll_ms,
    }


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
        "lmstudio": lm,
        "mcp_enabled": env.mcp_enabled,
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


@router.post("/collections", status_code=201)
async def create_collection(body: CollectionCreate):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "nome vazio")
    try:
        cfg = await resolve_ingest_config(None, body.ingest_config)
    except ConfigError as e:
        raise HTTPException(400, str(e))
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO collections(id, name, description, ingest_config)
               VALUES (%s,%s,%s,%s::jsonb) ON CONFLICT (name) DO NOTHING RETURNING *""",
            (str(uuid.uuid4()), name, body.description, json.dumps(cfg)))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(409, f"coleção '{name}' já existe")
    await db.event(f"Coleção criada: {name}", collection_id=row["id"])
    return row


@router.patch("/collections/{col_id}")
async def patch_collection(col_id: str, body: CollectionPatch):
    col_id = _uuid(col_id)
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
async def list_documents(collection_id: str | None = None, status: str | None = None,
                         q: str | None = None, limit: int = Query(100, le=500),
                         offset: int = 0):
    where, params = [], []
    if collection_id:
        where.append("d.collection_id=%s"); params.append(_uuid(collection_id))
    if status:
        where.append("d.status=%s"); params.append(status)
    if q:
        where.append("(d.filename ILIKE %s OR d.source_path ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"""SELECT d.*, c.name AS collection FROM documents d
                JOIN collections c ON c.id=d.collection_id {clause}
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
            """SELECT d.*, c.name AS collection FROM documents d
               JOIN collections c ON c.id=d.collection_id WHERE d.id=%s""", (doc_id,))
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
    collection_id = _uuid(collection_id)
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, ingest_config, embedding_model FROM collections WHERE id=%s",
            (collection_id,),
        )
        collection = await cur.fetchone()
        if not collection:
            raise HTTPException(404, "coleção não encontrada")

    eff = await settings.effective()
    default_model = await settings.get("embedding_model")
    auto_start = should_auto_start_upload(
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
                           stored_path, content_sha256, size_bytes, status, ingest_config)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                       ON CONFLICT (collection_id, content_sha256) DO NOTHING RETURNING id""",
                    (
                        doc_id, collection_id, filename, rel, blob_rel, digest, size,
                        initial_status,
                        json.dumps(auto_config) if auto_config else None,
                    ))
                row = await cur.fetchone()
                if row:
                    results.append({"filename": filename, "source_path": rel,
                                    "status": initial_status, "document_id": doc_id,
                                    "size_bytes": size})
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
        await db.event(
            f"{queued} documento(s) importado(s) e enfileirado(s) automaticamente",
            collection_id=collection_id,
        )
    if staged:
        await db.event(
            f"{staged} documento(s) importado(s), aguardando configuração de ingestão",
            collection_id=collection_id,
        )
    return {
        "results": results,
        "staged": staged,
        "queued": queued,
        "duplicates": sum(1 for r in results if r["status"] == "duplicate"),
        "ignored_empty": sum(1 for r in results if r["status"] == "ignored_empty"),
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
    model = body.model or await settings.get("llm_model")
    if not model:
        raise HTTPException(400, "select an LLM model before classifying")
    eff = await settings.effective()
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT d.*, c.name AS collection FROM documents d
               JOIN collections c ON c.id=d.collection_id WHERE d.id=%s""",
            (doc_id,),
        )
        doc = await cur.fetchone()
        if not doc:
            raise HTTPException(404, "document not found")
        cur = await conn.execute(
            """SELECT c.id, c.name, c.description,
                      (SELECT count(*) FROM documents x WHERE x.collection_id=c.id) AS doc_count
               FROM collections c ORDER BY c.name"""
        )
        collections = await cur.fetchall()
    try:
        extracted = await _sample_extract(doc)
        client = await settings.lmstudio_client(timeout=eff["auto"]["request_timeout"], retries=1)
        result = await classify_document(
            client, model, filename=doc["filename"], source_path=doc["source_path"],
            text=extracted.full_text(), collections=collections,
            sample_chars=eff["auto"]["sample_chars"],
            timeout=eff["auto"]["request_timeout"],
        )
    except ExtractionError as e:
        raise HTTPException(422, f"could not extract document sample: {e}") from e
    except LMStudioError as e:
        raise HTTPException(503, f"LLM unavailable: {e}") from e
    except ValueError as e:
        raise HTTPException(502, f"invalid classifier response: {e}") from e
    result.update({
        "document_id": doc_id,
        "filename": doc["filename"],
        "current_collection": doc["collection"],
    })
    return result


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
            await conn.execute("DELETE FROM chunks WHERE document_id=%s", (doc_id,))
            await conn.execute(
                """UPDATE documents SET status='pending', error=NULL, retry_at=NULL,
                       attempts=0, chunks_total=0, chunks_embedded=0,
                       ingest_config=%s::jsonb, updated_at=now() WHERE id=%s""",
                (json.dumps(cfg), doc_id))
    extraction_cache_path(doc_id).unlink(missing_ok=True)
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
        return await semantic_search(
            body.query,
            body.collections,
            body.top_k,
            body.min_score,
            body.filters(),
        )
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
