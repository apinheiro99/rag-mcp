"""Persistent CPU evidence graph over deterministic named-concept mentions.

This intentionally avoids query-time LLM/GPU work. Mentions are materialized during
indexing and lazily backfilled for older documents.
"""
from __future__ import annotations

from collections import defaultdict
import asyncio
import logging
import re
from typing import Any

from .db import db
from .settings import settings

log = logging.getLogger("rag.graph_retrieval")

CONCEPT = re.compile(r"\b(?:[A-Z][A-Za-z0-9]{2,}(?:\s+[A-Z][A-Za-z0-9]{2,})*|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")


def extract_concepts(text: str) -> set[str]:
    return {term.casefold() for term in CONCEPT.findall(text or "")}


def _assemble(rows: list[dict[str, Any]], mentions: dict[str, set[str]], query: str, limit: int, *, persistent: bool) -> dict[str, Any]:
    nodes = {str(r["chunk_id"]): r for r in rows}
    inverted: dict[str, set[str]] = defaultdict(set)
    for chunk_id, concepts in mentions.items():
        if chunk_id in nodes:
            for concept in concepts:
                inverted[concept].add(chunk_id)

    query_terms = extract_concepts(query)
    edges: list[dict[str, Any]] = []
    for concept, ids in sorted(inverted.items()):
        ordered = sorted(ids)
        if len(ordered) < 2:
            continue
        for i, source in enumerate(ordered):
            for target in ordered[i + 1:]:
                if nodes[source]["document_id"] == nodes[target]["document_id"]:
                    continue
                edges.append({
                    "source": source,
                    "target": target,
                    "concept": concept,
                    "type": "shared_mention",
                    "query_match": concept in query_terms,
                })
    edges.sort(key=lambda e: (not e["query_match"], e["concept"], e["source"], e["target"]))
    return {
        "experimental": True,
        "persistent": persistent,
        "nodes": [
            {"chunk_id": key, "document": row["document"], "location": row["location"]}
            for key, row in nodes.items()
        ],
        "edges": edges[:limit],
    }


def evidence_graph(rows, query, limit):
    mentions = {str(r["chunk_id"]): extract_concepts(r["content"]) for r in rows}
    return _assemble(rows, mentions, query, limit, persistent=False)


async def rebuild_document_mentions(document_id: str) -> int:
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, content FROM chunks WHERE document_id=%s ORDER BY seq",
            (document_id,),
        )
        chunks = await cur.fetchall()
        rows: list[tuple[str, str, str]] = []
        for chunk in chunks:
            for concept in extract_concepts(chunk["content"]):
                rows.append((str(chunk["id"]), document_id, concept))
        async with conn.transaction():
            await conn.execute("DELETE FROM graph_mentions WHERE document_id=%s", (document_id,))
            if rows:
                cur = conn.cursor()
                await cur.executemany(
                    "INSERT INTO graph_mentions(chunk_id,document_id,concept) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    rows,
                )
        return len(rows)


async def persistent_evidence_graph(rows: list[dict[str, Any]], query: str, limit: int) -> dict[str, Any]:
    if not rows:
        return _assemble(rows, {}, query, limit, persistent=True)

    chunk_ids = [str(r["chunk_id"]) for r in rows]
    document_ids = sorted({str(r["document_id"]) for r in rows})

    async def fetch_mentions() -> dict[str, set[str]]:
        async with db.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT chunk_id, concept FROM graph_mentions WHERE chunk_id=ANY(%s::uuid[])",
                (chunk_ids,),
            )
            found: dict[str, set[str]] = defaultdict(set)
            for item in await cur.fetchall():
                found[str(item["chunk_id"])].add(item["concept"])
            return found

    mentions = await fetch_mentions()
    if not mentions:
        graph = _assemble(rows, {}, query, limit, persistent=True)
        graph["available"] = False
        graph["reason"] = "graph_not_materialized_yet"
        return graph

    graph = _assemble(rows, mentions, query, limit, persistent=True)
    graph["available"] = True
    return graph


class GraphWorker:
    def __init__(self) -> None:
        self._task = None
        self._stop = None
        self.current_document: str | None = None
        self.indexed_documents = 0
        self.last_error: str | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="graph-evidence-worker")

    async def stop(self) -> None:
        if not self._task:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _next_stale_document(self) -> str | None:
        async with db.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT id::text AS id
                   FROM documents
                   WHERE status='ready'
                     AND processed_at IS NOT NULL
                     AND (graph_indexed_at IS NULL OR graph_indexed_at < processed_at)
                   ORDER BY processed_at, id
                   LIMIT 1"""
            )
            row = await cur.fetchone()
            return str(row["id"]) if row else None

    async def _mark_indexed(self, document_id: str) -> None:
        async with db.pool.connection() as conn:
            await conn.execute(
                "UPDATE documents SET graph_indexed_at=now() WHERE id=%s",
                (document_id,),
            )

    async def _poll_interval(self) -> float:
        effective = await settings.effective()
        return max(0.1, float(effective.get("graph", {}).get("poll_interval_seconds", 10)))

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                document_id = await self._next_stale_document()
                if not document_id:
                    await asyncio.sleep(await self._poll_interval())
                    continue
                self.current_document = document_id
                await rebuild_document_mentions(document_id)
                await self._mark_indexed(document_id)
                self.indexed_documents += 1
                self.last_error = None
                self.current_document = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.current_document = None
                log.exception("graph worker iteration failed; retrying")
                await asyncio.sleep(await self._poll_interval())


graph_worker = GraphWorker()
