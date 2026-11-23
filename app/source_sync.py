"""Collection directory sources and incremental synchronization."""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from .config import env
from .db import db
from .ingest import extraction_cache_path, resolve_ingest_config
from .ingest_policy import should_auto_start_upload, should_ignore_empty_upload
from .settings import settings

log = logging.getLogger("rag.sources")


def validate_interval(interval_seconds: int, source_cfg: dict[str, Any]) -> int:
    allowed = [int(x) for x in source_cfg.get("interval_options_seconds", [])]
    value = int(interval_seconds)
    if not allowed or value not in allowed:
        raise ValueError(f"interval must be one of: {allowed}")
    return value


def is_excluded(relative_path: str, patterns: list[str]) -> bool:
    rel = relative_path.replace("\\", "/").lstrip("/")
    parts = rel.split("/")
    for pattern in patterns:
        p = str(pattern).replace("\\", "/").lstrip("/")
        if fnmatch.fnmatch(rel, p):
            return True
        if p.endswith("/**") and p[:-3] in parts:
            return True
    return False


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _copy_blob(path: Path, digest: str) -> str:
    blob_rel = f"blobs/{digest[:2]}/{digest}"
    blob = env.data_dir / blob_rel
    blob.parent.mkdir(parents=True, exist_ok=True)
    if not blob.exists():
        shutil.copy2(path, blob)
    return blob_rel


async def sync_collection_source(source_id: str) -> dict[str, Any]:
    eff = await settings.effective()
    source_cfg = eff.get("sources", {})
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT s.*, c.ingest_config AS collection_config,
                      c.embedding_model AS collection_model
               FROM collection_sources s JOIN collections c ON c.id=s.collection_id
               WHERE s.id=%s""",
            (source_id,),
        )
        source = await cur.fetchone()
    if not source:
        raise ValueError("source not found")

    root = Path(source["path"]).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"source path is not a readable directory: {root}")

    allowed = {str(x).lower() for x in eff["allowed_extensions"]}
    max_bytes = int(eff["ingest"]["max_file_mb"]) * 1024 * 1024
    patterns = [str(x) for x in (source.get("exclude_patterns") or [])]
    default_patterns = [str(x) for x in source_cfg.get("default_exclude_patterns", [])]
    patterns = list(dict.fromkeys(default_patterns + patterns))
    default_model = await settings.get("embedding_model")
    auto_start = should_auto_start_upload(
        enabled=bool(eff.get("workflow", {}).get("auto_start_configured_uploads", False)),
        collection_config=source["collection_config"],
        collection_model=source["collection_model"],
        default_model=default_model,
    )
    auto_config = None
    if auto_start:
        auto_config = await resolve_ingest_config(source["collection_config"], None)
        auto_config["embedding_model"] = source["collection_model"] or default_model

    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT * FROM documents WHERE source_id=%s", (source_id,)
        )
        existing = {row["source_path"]: row for row in await cur.fetchall()}

    stats = {"new": 0, "changed": 0, "deleted": 0, "unchanged": 0, "ignored": 0, "errors": 0}
    seen: set[str] = set()
    for path in sorted(root.rglob("*")):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                continue
            rel = resolved.relative_to(root).as_posix()
            if is_excluded(rel, patterns):
                stats["ignored"] += 1
                continue
            if path.suffix.lower() not in allowed:
                stats["ignored"] += 1
                continue
            size = path.stat().st_size
            if should_ignore_empty_upload(size_bytes=size) or size > max_bytes:
                stats["ignored"] += 1
                continue
            digest, size = await asyncio.to_thread(_sha256_file, path)
            seen.add(rel)
            old = existing.get(rel)
            if old and old["content_sha256"] == digest:
                stats["unchanged"] += 1
                continue
            blob_rel = await asyncio.to_thread(_copy_blob, path, digest)
            initial_status = "pending" if auto_start else "staged"
            mtime_ns = path.stat().st_mtime_ns
            async with db.pool.connection() as conn:
                cur = await conn.execute(
                    """SELECT id FROM documents
                       WHERE collection_id=%s AND content_sha256=%s AND (%s::uuid IS NULL OR id<>%s::uuid)""",
                    (source["collection_id"], digest, str(old["id"]) if old else None,
                     str(old["id"]) if old else None),
                )
                duplicate = await cur.fetchone()
                if duplicate:
                    stats["errors"] += 1
                    continue
                if old:
                    await conn.execute("DELETE FROM chunks WHERE document_id=%s", (old["id"],))
                    await conn.execute(
                        """UPDATE documents SET filename=%s, stored_path=%s, content_sha256=%s,
                               size_bytes=%s, source_mtime_ns=%s, status=%s, error=NULL,
                               attempts=0, retry_at=NULL, chunks_total=0, chunks_embedded=0,
                               ingest_config=%s::jsonb, updated_at=now(), processed_at=NULL
                           WHERE id=%s""",
                        (path.name, blob_rel, digest, size, mtime_ns, initial_status,
                         json.dumps(auto_config) if auto_config else None, old["id"]),
                    )
                    extraction_cache_path(str(old["id"])).unlink(missing_ok=True)
                    stats["changed"] += 1
                else:
                    doc_id = str(uuid.uuid4())
                    await conn.execute(
                        """INSERT INTO documents(
                               id, collection_id, filename, source_path, stored_path,
                               content_sha256, size_bytes, status, ingest_config,
                               source_id, source_mtime_ns)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)""",
                        (doc_id, source["collection_id"], path.name, rel, blob_rel, digest,
                         size, initial_status, json.dumps(auto_config) if auto_config else None,
                         source_id, mtime_ns),
                    )
                    stats["new"] += 1
        except Exception:
            log.exception("source sync failed for %s", path)
            stats["errors"] += 1

    for rel, old in existing.items():
        if rel in seen:
            continue
        async with db.pool.connection() as conn:
            await conn.execute("DELETE FROM documents WHERE id=%s", (old["id"],))
        extraction_cache_path(str(old["id"])).unlink(missing_ok=True)
        stats["deleted"] += 1

    async with db.pool.connection() as conn:
        await conn.execute(
            """UPDATE collection_sources SET last_sync_at=now(),
                   next_sync_at=now() + (interval_seconds * interval '1 second'),
                   last_error=NULL, last_stats=%s::jsonb, updated_at=now()
               WHERE id=%s""",
            (json.dumps(stats), source_id),
        )
    await db.event(
        f"Directory source synced: {root} (new={stats['new']}, changed={stats['changed']}, deleted={stats['deleted']})",
        collection_id=source["collection_id"],
    )
    return stats


class SourceSyncWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="rag-source-sync")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                eff = await settings.effective()
                source_cfg = eff["sources"]
                poll = max(1, int(source_cfg["poll_interval_seconds"]))
                async with db.pool.connection() as conn:
                    cur = await conn.execute(
                        """SELECT id FROM collection_sources
                           WHERE enabled AND (next_sync_at IS NULL OR next_sync_at <= now())
                           ORDER BY coalesce(next_sync_at, created_at) LIMIT 1"""
                    )
                    row = await cur.fetchone()
                if row:
                    sid = str(row["id"])
                    try:
                        await sync_collection_source(sid)
                    except Exception as e:
                        log.exception("directory source sync failed: %s", sid)
                        async with db.pool.connection() as conn:
                            await conn.execute(
                                """UPDATE collection_sources SET last_error=%s,
                                       next_sync_at=now() + (interval_seconds * interval '1 second'),
                                       updated_at=now() WHERE id=%s""",
                                (str(e), sid),
                            )
                    continue
                await asyncio.sleep(poll)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("source sync worker loop failed")
                eff = await settings.effective()
                await asyncio.sleep(max(1, int(eff["sources"]["error_retry_seconds"])))


source_worker = SourceSyncWorker()
