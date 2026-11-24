"""Busca semântica sobre o pgvector com filtros e observabilidade."""
from __future__ import annotations

import logging
import time
from typing import Any

from .db import db, vector_literal, MAX_HNSW_HALFVEC_DIM
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
            # Exact retrieval remains available for selective filters or explicit opt-out.
            binary = (dim > MAX_HNSW_HALFVEC_DIM and scfg.get("binary_hnsw_enabled")
                      and not filters.active())
            if binary:
                pool = max(top_k, top_k * int(scfg["binary_candidate_multiplier"]))
                cur = await conn.execute(
                    f"""WITH candidates AS MATERIALIZED (
                        SELECT v.chunk_id
                        FROM {table} v
                        JOIN chunks c ON c.id=v.chunk_id
                        JOIN documents d ON d.id=c.document_id
                        WHERE c.collection_id=ANY(%s::uuid[]) AND d.status='ready' AND c.embedded
                        ORDER BY binary_quantize(v.embedding)::bit({dim})
                            <~> binary_quantize(%s::{coltype})::bit({dim})
                        LIMIT %s
                    )
                    SELECT c.id AS chunk_id, c.seq, c.content, c.loc,
                           d.id AS document_id, d.filename, d.source_path, d.meta,
                           col.name AS collection,
                           1 - (v.embedding <=> %s::{coltype}) AS score
                    FROM candidates a JOIN {table} v ON v.chunk_id=a.chunk_id
                    JOIN chunks c ON c.id=v.chunk_id
                    JOIN documents d ON d.id=c.document_id
                    JOIN collections col ON col.id=c.collection_id
                    ORDER BY score DESC, c.id LIMIT %s""",
                    (col_ids, lit, pool, lit, top_k),
                )
                rows = await cur.fetchall()
                # HNSW post-filtering may exhaust the scan; preserve completeness.
                binary = len(rows) >= top_k
            if not binary:
                cur = await conn.execute(
                    f"""SELECT c.id AS chunk_id, c.seq, c.content, c.loc,
                               d.id AS document_id, d.filename, d.source_path, d.meta,
                               col.name AS collection,
                               1 - (v.embedding <=> %s::{coltype}) AS score
                        FROM {table} v
                        JOIN chunks c ON c.id = v.chunk_id
                        JOIN documents d ON d.id = c.document_id
                        JOIN collections col ON col.id = c.collection_id
                        WHERE c.collection_id = ANY(%s::uuid[]) AND d.status='ready' AND c.embedded{where_filter}
                        ORDER BY v.embedding <=> %s::{coltype}
                        LIMIT %s""",
                    (lit, col_ids, *filter_params, lit, top_k),
                )
                rows = await cur.fetchall()
    db_ms = (time.perf_counter() - t0) * 1000
    return rows, embedding_ms, db_ms



def should_apply_lexical(dense: list[dict], candidate_k: int, scfg: dict) -> tuple[bool, str | None]:
    """Use lexical retrieval only when dense retrieval is weak or undersupplied."""
    if not scfg.get("hybrid_enabled"):
        return False, "disabled"
    if not dense:
        return True, None
    min_candidates = max(1, min(candidate_k, int(scfg.get("hybrid_min_dense_candidates", candidate_k))))
    if len(dense) < min_candidates:
        return True, None
    threshold = float(scfg.get("hybrid_min_dense_score", 0.55))
    if float(dense[0].get("score", 0.0)) < threshold:
        return True, None
    return False, "dense_sufficient"

def fuse_rankings(rankings: list[list[dict]], rank_constant: int) -> list[dict]:
    """Reciprocal rank fusion; never equate lexical and cosine scores."""
    merged = {}
    for ranking in rankings:
        seen = set()
        for rank, row in enumerate(ranking, 1):
            key = str(row["chunk_id"])
            if key in seen:
                continue
            seen.add(key)
            item = merged.setdefault(key, {**row, "fusion_score": 0.0})
            if "lexical_score" in row:
                item["lexical_score"] = row["lexical_score"]
            item["fusion_score"] += 1 / (rank_constant + rank)
    return sorted(merged.values(), key=lambda x: (-x["fusion_score"], str(x["chunk_id"])))


async def _lexical_search(query, cols, filters, limit):
    filter_sql, params = filters.sql()
    extra = f" AND {filter_sql}" if filter_sql else ""
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"""SELECT c.id AS chunk_id, c.seq, c.content, c.loc,
                       d.id AS document_id, d.filename, d.source_path, d.meta,
                       col.name AS collection,
                       ts_rank_cd(to_tsvector('english', c.content), q.term) AS lexical_score
                FROM chunks c JOIN documents d ON d.id=c.document_id
                JOIN collections col ON col.id=c.collection_id
                CROSS JOIN (SELECT to_tsquery('english', string_agg(quote_literal(lexeme), ' | ')) AS term
                            FROM unnest(tsvector_to_array(to_tsvector('english', %s))) AS t(lexeme)) q
                WHERE c.collection_id = ANY(%s::uuid[]) AND c.embedded
                  AND d.status='ready'
                  AND to_tsvector('english', c.content) @@ q.term {extra}
                ORDER BY lexical_score DESC, c.id LIMIT %s""",
            (query, [str(c["id"]) for c in cols], *params, limit),
        )
        return await cur.fetchall()


def _format_result(r):
    return {
        "chunk_id": str(r["chunk_id"]), "document_id": str(r["document_id"]),
        "content": r["content"], "score": round(float(r.get("score", 0)), 4),
        "collection": r["collection"], "document": r["filename"],
        "source_path": r["source_path"], "location": r["loc"],
        "location_label": location_label(r["loc"]), "chunk_seq": r["seq"],
        "metadata": r["meta"],
        **({"lexical_score": float(r["lexical_score"])} if "lexical_score" in r else {}),
    }


def is_heading_only(row: dict) -> bool:
    heading = row.get("location", {}).get("heading")
    return bool(heading and " ".join(row["content"].split()).casefold() ==
                " ".join(str(heading).split()).casefold())


async def _expand_headings(rows, max_chars):
    """Attach adjacent text from the same section, keeping both chunk references."""
    targets = [r["chunk_id"] for r in rows if is_heading_only(r)]
    if not targets:
        return
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT a.id AS anchor_id, b.id AS neighbor_id, b.content, b.loc, b.seq
               FROM chunks a JOIN chunks b
                 ON b.document_id=a.document_id AND b.seq=a.seq+1
               WHERE a.id=ANY(%s::uuid[]) AND b.embedded
                 AND a.loc->>'heading'=b.loc->>'heading'
                 AND a.loc->>'chapter_index' IS NOT DISTINCT FROM b.loc->>'chapter_index'""",
            (targets,),
        )
        neighbors = {str(r["anchor_id"]): r for r in await cur.fetchall()}
    for row in rows:
        neighbor = neighbors.get(row["chunk_id"])
        if neighbor:
            row["context"] = {"chunk_id": str(neighbor["neighbor_id"]),
                              "chunk_seq": neighbor["seq"],
                              "location": neighbor["loc"],
                              "content": neighbor["content"][:max_chars]}
            row["content"] += "\n\n" + row["context"]["content"]


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
        candidate_k = max(top_k, min(
            int(rcfg.get("max_candidates", 32)),
            max(top_k, top_k * int(rcfg.get("candidate_multiplier", 4))),
        ))

    started = time.perf_counter()
    embedding_ms = 0.0
    db_ms = 0.0
    dense_ms = lexical_ms = heading_ms = 0.0
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

        hybrid_requested = bool(scfg.get("hybrid_enabled")) and min_score <= 0
        hybrid = False
        lexical_skipped_reason = None
        adaptive = bool(scfg.get("adaptive_enabled")) and bool(rcfg.get("enabled"))
        expanded = False
        async def execute_search_pipeline():
            nonlocal embedding_ms, db_ms, results, expanded, dense_ms, lexical_ms, heading_ms, hybrid, lexical_skipped_reason
            dense = []
            lexical = []
            depth = max(candidate_k, int(scfg["adaptive_candidate_limit"])) if adaptive else candidate_k
            for (model, dim), group_cols in groups.items():
                rows, emb_elapsed, db_elapsed = await _search_group(
                    query=query, model=model, dim=dim, group_cols=group_cols,
                    filters=filters, top_k=depth, scfg=scfg, client=client,
                )
                embedding_ms += emb_elapsed
                db_ms += db_elapsed
                dense_ms += db_elapsed
                dense.extend(_format_result(r) for r in rows if float(r["score"]) >= min_score)
            dense.sort(key=lambda x: x["score"], reverse=True)
            if hybrid_requested:
                hybrid, lexical_skipped_reason = should_apply_lexical(dense, candidate_k, scfg)
            if hybrid:
                t = time.perf_counter()
                lexical = [_format_result(r) for r in await _lexical_search(
                    query, cols, filters, int(scfg["lexical_candidates"]))]
                lexical_ms += (time.perf_counter()-t)*1000
                db_ms += lexical_ms
                results = fuse_rankings([dense, lexical], int(scfg["fusion_rank_constant"]))
            else:
                results = dense
            if scfg.get("expand_heading_context"):
                t = time.perf_counter()
                await _expand_headings(results, int(scfg["heading_context_chars"]))
                heading_ms = (time.perf_counter()-t)*1000
            rerank_pool = results[:candidate_k]
            rerank_started = time.perf_counter()
            rerank_error = None
            reranked = rerank_pool
            if rcfg.get("enabled") and rerank_pool:
                try:
                    cfg = {**rcfg, "model": reranker_model}
                    reranked = await rerank_results(query, rerank_pool, cfg, client)
                    if (adaptive and reranked and
                        reranked[0].get("rerank_score", 1) < float(scfg["adaptive_min_rerank_score"])):
                        extra = results[candidate_k:depth]
                        if extra:
                            second = await rerank_results(query, extra, cfg, client)
                            rerank_pool = rerank_pool + extra
                            reranked = sorted(reranked + second,
                                key=lambda x: (x["rerank_score"], x["score"]), reverse=True)
                            expanded = True
                except Exception as exc:
                    rerank_error = f"{type(exc).__name__}: {exc}"
                    if not rcfg.get("fail_open", True):
                        raise
                    log.warning("rerank failed; using retrieval ranking: %s", rerank_error)
            rerank_ms = (time.perf_counter() - rerank_started) * 1000 if rcfg.get("enabled") and rerank_pool else 0.0
            return reranked[:top_k], rerank_pool, rerank_ms, rerank_error

        await runtime_scheduler.ensure_resident(client, requirements)
        final_results, rerank_pool, rerank_ms, rerank_error = await execute_search_pipeline()
        response = {
            "query": query,
            "results": final_results,
            "collections_searched": [c["name"] for c in cols],
            "filters_applied": filters.to_dict(),
            "total_candidates": len(results),
            "retrieval": {"hybrid_requested": hybrid_requested, "hybrid": hybrid, "lexical_skipped_reason": lexical_skipped_reason, "expanded": expanded,
                          "timings_ms": {"dense": round(dense_ms, 2), "lexical": round(lexical_ms, 2),
                                         "heading": round(heading_ms, 2), "embedding": round(embedding_ms, 2)}},
            "rerank": {
                "enabled": bool(rcfg.get("enabled")),
                "applied": bool(rcfg.get("enabled") and rerank_pool and not rerank_error),
                "candidate_count": len(rerank_pool),
                "duration_ms": round(rerank_ms, 2),
                "error": rerank_error,
            },
        }
        trace_id = await record_retrieval({
            "query": query,
            "collections": [c["name"] for c in cols],
            "filters": filters.to_dict(),
            "top_k": top_k,
            "min_score": min_score,
            "embedding_ms": embedding_ms,
            "db_ms": db_ms,
            "dense_ms": dense_ms, "lexical_ms": lexical_ms, "heading_ms": heading_ms,
            "total_ms": (time.perf_counter() - started) * 1000,
            "candidate_count": len(results),
            "result_count": len(final_results),
            "top_score": final_results[0]["score"] if final_results else None,
        })
        response["trace_id"] = trace_id
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
            "dense_ms": dense_ms, "lexical_ms": lexical_ms, "heading_ms": heading_ms,
            "total_ms": (time.perf_counter() - started) * 1000,
            "candidate_count": len(results),
            "result_count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        })
        raise
