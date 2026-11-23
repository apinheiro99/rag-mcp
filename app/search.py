"""Busca semântica sobre o pgvector com filtros e observabilidade."""
from __future__ import annotations

import logging
import time
from typing import Any

from .db import db, vector_literal
from .retrieval_observability import record_retrieval
from .model_runtime import ModelRequirement, runtime_scheduler
from .rerank import rerank_results
from .search_filters import SearchFilters
from .settings import settings

log = logging.getLogger("rag.search")


def location_label(loc: dict) -> str:
    parts = []
    if loc.get("page"):
        parts.append(f"página {loc['page']}")
    if loc.get("chapter"):
        parts.append(f"capítulo: {loc['chapter']}")
    if loc.get("heading"):
        parts.append(f"seção: {loc['heading']}")
    if loc.get("table"):
        parts.append("tabela")
    return "; ".join(parts)


class SearchError(Exception):
    pass


async def _resolve_collections(
    identifiers: list[str] | None,
    filters: SearchFilters,
) -> list[dict]:
    async with db.pool.connection() as conn:
        if identifiers:
            cur = await conn.execute(
                """SELECT id, name, embedding_model, embedding_dim, default_search FROM collections
                   WHERE name = ANY(%s) OR id::text = ANY(%s)""",
                (identifiers, identifiers),
            )
            rows = await cur.fetchall()
            found = {r["name"] for r in rows} | {str(r["id"]) for r in rows}
            missing = [i for i in identifiers if i not in found]
            if missing:
                raise SearchError(f"coleções não encontradas: {', '.join(missing)}")
        elif filters.document_ids:
            cur = await conn.execute(
                """SELECT DISTINCT c.id, c.name, c.embedding_model, c.embedding_dim, c.default_search
                   FROM collections c
                   JOIN documents d ON d.collection_id=c.id
                   WHERE d.id = ANY(%s::uuid[])""",
                (filters.document_ids,),
            )
            rows = await cur.fetchall()
        else:
            cur = await conn.execute(
                """SELECT id, name, embedding_model, embedding_dim, default_search FROM collections
                   WHERE embedding_model IS NOT NULL AND default_search=TRUE"""
            )
            rows = await cur.fetchall()
    return [r for r in rows if r["embedding_model"] and r["embedding_dim"]]


async def _search_group(
    *,
    query: str,
    model: str,
    dim: int,
    group_cols: list[dict],
    filters: SearchFilters,
    top_k: int,
    scfg: dict,
    client,
) -> tuple[list[dict], float, float]:
    table_type = await db.vector_table_for_dim(dim)
    if not table_type:
        return [], 0.0, 0.0

    table, coltype = table_type

    t0 = time.perf_counter()
    qvec = (await client.embed([query], model))[0]
    embedding_ms = (time.perf_counter() - t0) * 1000
    if len(qvec) != dim:
        raise SearchError(
            f"modelo {model} retornou dimensão {len(qvec)}; "
            f"coleção espera {dim}. Reindexe a coleção."
        )

    lit = vector_literal(qvec)
    col_ids = [str(c["id"]) for c in group_cols]
    filter_sql, filter_params = filters.sql()
    where_filter = f" AND {filter_sql}" if filter_sql else ""

    t0 = time.perf_counter()
    async with db.pool.connection() as conn:
        async with conn.transaction():
            try:
                await conn.execute(
                    f"SET LOCAL hnsw.ef_search = {int(scfg['hnsw_ef_search'])}"
                )
                await conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
            except Exception:
                pass
            cur = await conn.execute(
                f"""SELECT c.id AS chunk_id, c.seq, c.content, c.loc,
                           d.id AS document_id, d.filename, d.source_path, d.meta,
                           col.name AS collection,
                           1 - (v.embedding <=> %s::{coltype}) AS score
                    FROM {table} v
                    JOIN chunks c ON c.id = v.chunk_id
                    JOIN documents d ON d.id = c.document_id
                    JOIN collections col ON col.id = c.collection_id
                    WHERE c.collection_id = ANY(%s::uuid[]){where_filter}
                    ORDER BY v.embedding <=> %s::{coltype}
                    LIMIT %s""",
                (lit, col_ids, *filter_params, lit, top_k),
            )
            rows = await cur.fetchall()
    db_ms = (time.perf_counter() - t0) * 1000
    return rows, embedding_ms, db_ms


async def semantic_search(
    query: str,
    collections: list[str] | None = None,
    top_k: int | None = None,
    min_score: float | None = None,
    filters: SearchFilters | None = None,
) -> dict[str, Any]:
    if not query or not query.strip():
        raise SearchError("query vazia")

    filters = filters or SearchFilters()
    try:
        filters.validate()
    except ValueError as exc:
        raise SearchError(str(exc)) from exc

    eff = await settings.effective()
    scfg = eff["search"]
    rcfg = eff.get("rerank", {})
    top_k = int(top_k or scfg["top_k"])
    top_k = max(1, min(top_k, scfg["max_top_k"]))
    min_score = float(min_score if min_score is not None else scfg["min_score"])
    candidate_k = top_k
    if rcfg.get("enabled"):
        candidate_k = min(
            int(rcfg.get("max_candidates", 32)),
            max(top_k, top_k * int(rcfg.get("candidate_multiplier", 4))),
        )

    started = time.perf_counter()
    embedding_ms = 0.0
    db_ms = 0.0
    results: list[dict] = []
    cols: list[dict] = []

    try:
        cols = await _resolve_collections(collections, filters)
        if not cols:
            response = {
                "query": query,
                "results": [],
                "collections_searched": [],
                "filters_applied": filters.to_dict(),
                "message": "Nenhuma coleção indexada disponível para os filtros informados.",
            }
            await record_retrieval({
                "query": query,
                "collections": [],
                "filters": filters.to_dict(),
                "top_k": top_k,
                "min_score": min_score,
                "total_ms": (time.perf_counter() - started) * 1000,
            })
            return response

        groups: dict[tuple[str, int], list[dict]] = {}
        for c in cols:
            groups.setdefault((c["embedding_model"], c["embedding_dim"]), []).append(c)

        client = await settings.lmstudio_client()
        reranker_model = await settings.get("reranker_model", rcfg.get("model")) if rcfg.get("enabled") else None
        context_length = int(rcfg.get("context_length", 8192))
        requirements = [ModelRequirement(model, context_length) for model, _ in sorted(groups)]
        if reranker_model:
            requirements.append(ModelRequirement(reranker_model, context_length))

        async def execute_search_pipeline():
            nonlocal embedding_ms, db_ms, results
            for (model, dim), group_cols in groups.items():
                rows, emb_elapsed, db_elapsed = await _search_group(
                    query=query,
                    model=model,
                    dim=dim,
                    group_cols=group_cols,
                    filters=filters,
                    top_k=candidate_k,
                    scfg=scfg,
                    client=client,
                )
                embedding_ms += emb_elapsed
                db_ms += db_elapsed
                for r in rows:
                    score = float(r["score"])
                    if score < min_score:
                        continue
                    results.append({
                        "chunk_id": str(r["chunk_id"]),
                        "document_id": str(r["document_id"]),
                        "content": r["content"],
                        "score": round(score, 4),
                        "collection": r["collection"],
                        "document": r["filename"],
                        "source_path": r["source_path"],
                        "location": r["loc"],
                        "location_label": location_label(r["loc"]),
                        "chunk_seq": r["seq"],
                        "metadata": r["meta"],
                    })

            results.sort(key=lambda x: x["score"], reverse=True)
            rerank_pool = results[:candidate_k]
            rerank_started = time.perf_counter()
            rerank_error = None
            reranked = rerank_pool
            if rcfg.get("enabled") and rerank_pool:
                try:
                    reranked = await rerank_results(
                        query, rerank_pool, {**rcfg, "model": reranker_model}, client
                    )
                except Exception as exc:
                    rerank_error = f"{type(exc).__name__}: {exc}"
                    if not rcfg.get("fail_open", True):
                        raise
                    log.warning("rerank failed; using vector ranking: %s", rerank_error)
            rerank_ms = (time.perf_counter() - rerank_started) * 1000 if rcfg.get("enabled") and rerank_pool else 0.0
            return reranked[:top_k], rerank_pool, rerank_ms, rerank_error

        final_results, rerank_pool, rerank_ms, rerank_error = await runtime_scheduler.run(
            "semantic-search", client, requirements, execute_search_pipeline
        )
        response = {
            "query": query,
            "results": final_results,
            "collections_searched": [c["name"] for c in cols],
            "filters_applied": filters.to_dict(),
            "total_candidates": len(results),
            "rerank": {
                "enabled": bool(rcfg.get("enabled")),
                "applied": bool(rcfg.get("enabled") and rerank_pool),
                "candidate_count": len(rerank_pool),
                "duration_ms": round(rerank_ms, 2),
                "error": rerank_error,
            },
        }
        await record_retrieval({
            "query": query,
            "collections": [c["name"] for c in cols],
            "filters": filters.to_dict(),
            "top_k": top_k,
            "min_score": min_score,
            "embedding_ms": embedding_ms,
            "db_ms": db_ms,
            "total_ms": (time.perf_counter() - started) * 1000,
            "candidate_count": len(results),
            "result_count": len(final_results),
            "top_score": final_results[0]["score"] if final_results else None,
        })
        return response
    except Exception as exc:
        await record_retrieval({
            "query": query,
            "collections": [c["name"] for c in cols],
            "filters": filters.to_dict(),
            "top_k": top_k,
            "min_score": min_score,
            "embedding_ms": embedding_ms,
            "db_ms": db_ms,
            "total_ms": (time.perf_counter() - started) * 1000,
            "candidate_count": len(results),
            "result_count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        })
        raise
