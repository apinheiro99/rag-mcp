"""Busca semântica sobre o pgvector com filtros e observabilidade."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from .db import db, vector_literal, MAX_HNSW_HALFVEC_DIM
from .chunking import estimate_tokens
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
    if loc.get("symbol"):
        parts.append(f"symbol: {loc['symbol']}")
    if loc.get("line_start"):
        line_end = loc.get("line_end", loc["line_start"])
        parts.append(f"lines {loc['line_start']}-{line_end}")
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
                        WHERE c.collection_id=ANY(%s::uuid[]) AND c.embedded
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
                    WHERE d.status='ready'
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



_QUESTION_STARTERS = {
    "what", "when", "where", "which", "who", "why", "how", "compare", "explain",
    "describe", "does", "do", "is", "are", "in",
}


def analyze_query_intent(query: str, scfg: dict) -> dict:
    """Detect queries where lexical/title evidence should complement dense retrieval."""
    if not scfg.get("hybrid_query_aware_enabled", True):
        return {"force_hybrid": False, "reasons": [], "metadata_terms": [], "lexical_terms": []}

    text = " ".join((query or "").split())
    reasons: list[str] = []
    metadata_terms: list[str] = []
    lexical_terms: list[str] = []

    quoted = [m.group(1).strip() for m in re.finditer(r'["“]([^"”]{2,100})["”]', text)]
    if quoted:
        reasons.append("quoted_phrase")
        metadata_terms.extend(quoted)
        lexical_terms.extend(quoted)

    tokens = re.findall(r"\b[A-Za-z][A-Za-z0-9_+.#:/-]*\b", text)
    technical = []
    for token in tokens:
        bare = token.strip(".,:;!?()[]{}")
        if (
            re.fullmatch(r"[A-Z]{2,}[A-Z0-9-]*", bare)
            or re.search(r"\d", bare)
            or any(ch in bare for ch in ("_", "::", "/", "#", "+"))
            or (len(bare) > 2 and any(ch.isupper() for ch in bare[1:]))
        ):
            technical.append(bare)
    if technical:
        reasons.append("technical_token")
        metadata_terms.extend(technical)
        lexical_terms.extend(technical)

    # Capture title-like phrases such as "RAG with Python Cookbook" or
    # "Domain-Driven Design" without requiring an LLM query planner.
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9+.#:/-]*", text)
    phrase: list[str] = []
    phrases: list[str] = []
    joiners = {"with", "of", "and", "the", "for", "in", "to"}
    for word in words + ["__END__"]:
        is_title = (
            word != "__END__"
            and (
                (word[0].isupper() and word.casefold() not in _QUESTION_STARTERS)
                or word.casefold() in joiners
            )
        )
        if is_title:
            phrase.append(word)
        else:
            while phrase and phrase[-1].casefold() in joiners:
                phrase.pop()
            meaningful = [w for w in phrase if w.casefold() not in joiners]
            if len(meaningful) >= 2:
                phrases.append(" ".join(phrase))
            phrase = []
    if phrases:
        reasons.append("title_like_phrase")
        metadata_terms.extend(phrases)

    concept = re.findall(
        r"\b([A-Z][A-Za-z0-9+-]*(?:\s+[A-Z][A-Za-z0-9+-]*){0,2}\s+"
        r"(?:pattern|Pattern|root|Root|isolation|Isolation))\b",
        text,
    )
    if concept:
        reasons.append("named_concept")
        metadata_terms.extend(concept)
        lexical_terms.extend(concept)

    clean_terms: list[str] = []
    for term in metadata_terms:
        term = re.sub(r"^(?:In|From|According to)\s+", "", term, flags=re.I).strip(" ,.:;!?")
        if len(term) < 2:
            continue
        if term.casefold() not in {x.casefold() for x in clean_terms}:
            clean_terms.append(term)
    max_terms = max(0, int(scfg.get("hybrid_metadata_terms_max", 4)))
    clean_terms = clean_terms[:max_terms]

    clean_lexical: list[str] = []
    for term in lexical_terms:
        term = term.strip(" ,.:;!?")
        if len(term) < 2:
            continue
        if term.casefold() not in {x.casefold() for x in clean_lexical}:
            clean_lexical.append(term)
    clean_lexical = clean_lexical[:max_terms]

    return {
        "force_hybrid": bool(reasons),
        "reasons": reasons,
        "metadata_terms": clean_terms,
        "lexical_terms": clean_lexical,
    }


def decompose_query(query: str, scfg: dict) -> dict:
    """Deterministically split only explicit compound comparison questions."""
    if not scfg.get("decomposition_enabled", True):
        return {"enabled": False, "reason": "disabled", "subqueries": []}

    text = " ".join((query or "").split())
    match = re.match(
        r"(?i)^compare\s+(.+?)\s+(?:with|versus|vs\.?)\s+(.+?)(?::\s*(.+))?[?]?$",
        text,
    )
    if not match:
        match = re.match(
            r"(?i)^how\s+does\s+(.+?)\s+differ\s+from\s+(.+?)(?:[?]$|$)",
            text,
        )
        if match:
            left, right = match.group(1).strip(), match.group(2).strip()
            subqueries = [
                f"What are the key characteristics of {left}?",
                f"What are the key characteristics of {right}?",
            ]
            return {
                "enabled": True,
                "reason": "explicit_difference",
                "subqueries": subqueries[: max(1, int(scfg.get("decomposition_max_subqueries", 2)))],
            }
        return {"enabled": False, "reason": "simple_query", "subqueries": []}

    left, right, suffix = match.group(1).strip(), match.group(2).strip(), (match.group(3) or "").strip()
    if suffix:
        subqueries = [f"{left}: {suffix}", f"{right}: {suffix}"]
    else:
        subqueries = [
            f"What are the key characteristics of {left}?",
            f"What are the key characteristics of {right}?",
        ]
    return {
        "enabled": True,
        "reason": "explicit_comparison",
        "subqueries": subqueries[: max(1, int(scfg.get("decomposition_max_subqueries", 2)))],
    }


def _score_for_utility(row: dict) -> float:
    return float(row.get("rerank_score", row.get("score", 0.0)) or 0.0)


def choose_adaptive_top_k(rows: list[dict], requested_top_k: int, scfg: dict, *, explicit: bool) -> tuple[int, dict]:
    """Choose returned evidence count from relevance, never exceeding requested/configured bounds."""
    allow_below_explicit = bool(scfg.get("adaptive_top_k_allow_below_explicit", True))
    if (
        not scfg.get("adaptive_top_k_enabled", False)
        or not rows
        or (explicit and not allow_below_explicit)
    ):
        return min(requested_top_k, len(rows)), {"enabled": False, "reason": "explicit_or_disabled"}
    hi = max(0, min(requested_top_k, int(scfg.get("adaptive_top_k_max", requested_top_k))))
    if not hi:
        return 0, {"enabled": True, "reason": "zero_ceiling"}
    lo = min(hi, max(1, int(scfg.get("adaptive_top_k_min", 3))))
    threshold = float(scfg.get("adaptive_top_k_min_score", 0.55))
    gap = float(scfg.get("adaptive_top_k_gap", 0.18))
    scores = [_score_for_utility(row) for row in rows[:hi]]
    chosen = min(lo, len(scores))
    for i in range(chosen, len(scores)):
        prev = scores[i - 1]
        cur = scores[i]
        if cur < threshold or prev - cur > gap + 1e-12:
            break
        chosen = i + 1
    return max(1, min(chosen, len(rows))), {
        "enabled": True,
        "reason": "relevance_curve",
        "min_score": threshold,
        "gap": gap,
        "scores": [round(x, 4) for x in scores],
    }


def retrieval_usefulness(rows: list[dict], scfg: dict) -> dict:
    """Heuristic evidence usefulness, not answer correctness."""
    if not rows:
        return {"score": 0.0, "label": "none", "useful_results": 0, "result_count": 0}
    threshold = float(scfg.get("usefulness_min_score", 0.55))
    min_tokens = max(1, int(scfg.get("usefulness_min_content_tokens", 40)))
    scores = [_score_for_utility(r) for r in rows]
    relevance = sum(scores[: min(3, len(scores))]) / min(3, len(scores))
    useful = sum(score >= threshold for score in scores)
    coverage = useful / len(rows)
    substantive = sum(estimate_tokens(str(r.get("content") or "")) >= min_tokens for r in rows) / len(rows)
    score = max(0.0, min(1.0, 0.7 * relevance + 0.2 * coverage + 0.1 * substantive))
    label = "high" if score >= 0.75 else "medium" if score >= 0.5 else "low"
    return {
        "score": round(score, 4), "label": label, "useful_results": useful,
        "result_count": len(rows), "relevance": round(relevance, 4),
        "coverage": round(coverage, 4), "substantive": round(substantive, 4),
        "note": "heuristic retrieval usefulness; not answer correctness",
    }


def annotate_selection(rows: list[dict], *, hybrid: bool, expanded: bool) -> None:
    for final_rank, row in enumerate(rows, 1):
        if row.get("decomposition_query"):
            method = "decomposition"
        elif hybrid and "metadata_score" in row:
            method = "hybrid_metadata"
        elif hybrid and "lexical_score" in row:
            method = "hybrid"
        else:
            method = "dense"
        rerank = row.get("rerank_score")
        reason = f"{method} retrieval"
        if rerank is not None:
            reason += f"; reranked to #{final_rank} with relevance {float(rerank):.4f}"
        else:
            reason += f"; selected at rank #{final_rank}"
        if row.get("context"):
            reason += "; adjacent heading context expanded"
        row["selection"] = {
            "final_rank": final_rank,
            "retrieval_rank": row.get("retrieval_rank"),
            "method": method,
            "dense_score": row.get("score"),
            "lexical_score": row.get("lexical_score"),
            "fusion_score": row.get("fusion_score"),
            "rerank_score": rerank,
            "context_expanded": bool(row.get("context")),
            "reason": reason,
        }


def token_cost_estimate(query: str, rerank_pool: list[dict], final_rows: list[dict], rcfg: dict) -> dict:
    """Model-independent approximation; local inference has no API-dollar charge."""
    max_chars = int(rcfg.get("max_chars_per_candidate", 800))
    query_tokens = estimate_tokens(query)
    rerank_input_tokens = sum(estimate_tokens(str(r.get("content") or "")[:max_chars]) for r in rerank_pool)
    result_tokens = sum(estimate_tokens(str(r.get("content") or "")) for r in final_rows)
    rerank_output_tokens = len(rerank_pool) if rcfg.get("enabled") else 0
    return {
        "provider": "local",
        "currency_cost_usd": 0.0,
        "estimated": True,
        "query_tokens": query_tokens,
        "rerank_input_tokens": rerank_input_tokens,
        "rerank_output_tokens": rerank_output_tokens,
        "returned_context_tokens": result_tokens,
        "total_model_input_tokens": query_tokens + rerank_input_tokens,
    }


def should_apply_lexical(
    query: str,
    dense: list[dict],
    candidate_k: int,
    scfg: dict,
    *,
    intent: dict | None = None,
) -> tuple[bool, str | None]:
    """Use lexical retrieval for weak dense results or explicit lexical intent."""
    if not scfg.get("hybrid_enabled"):
        return False, "disabled"
    intent = intent or analyze_query_intent(query, scfg)
    if intent.get("lexical_terms"):
        return True, "query_exact_terms"
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
            if "metadata_score" in row:
                item["metadata_score"] = max(
                    float(item.get("metadata_score", 0.0)),
                    float(row["metadata_score"]),
                )
            if row.get("decomposition_query") and not item.get("decomposition_query"):
                item["decomposition_query"] = row["decomposition_query"]
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
                CROSS JOIN (SELECT plainto_tsquery('english', %s) AS term) q
                WHERE c.collection_id = ANY(%s::uuid[]) AND c.embedded
                  AND d.status='ready'
                  AND to_tsvector('english', c.content) @@ q.term {extra}
                ORDER BY lexical_score DESC, c.id LIMIT %s""",
            (query, [str(c["id"]) for c in cols], *params, limit),
        )
        return await cur.fetchall()


async def _metadata_search(terms, cols, filters, limit):
    terms = [str(x).strip().casefold() for x in terms if str(x).strip()]
    if not terms or limit <= 0:
        return []
    filter_sql, params = filters.sql()
    extra = f" AND {filter_sql}" if filter_sql else ""
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"""WITH candidates AS (
                    SELECT c.id AS chunk_id, c.seq, c.content, c.loc,
                           d.id AS document_id, d.filename, d.source_path, d.meta,
                           col.name AS collection,
                           max(length(t.term))::double precision AS metadata_score
                    FROM documents d
                    JOIN collections col ON col.id=d.collection_id
                    JOIN chunks c ON c.document_id=d.id
                    JOIN unnest(%s::text[]) AS t(term)
                      ON lower(d.filename) LIKE ('%%' || t.term || '%%')
                      OR lower(d.source_path) LIKE ('%%' || t.term || '%%')
                    WHERE d.collection_id = ANY(%s::uuid[])
                      AND d.status='ready'
                      AND c.embedded{extra}
                    GROUP BY c.id, c.seq, c.content, c.loc,
                             d.id, d.filename, d.source_path, d.meta, col.name
                ),
                ranked AS (
                    SELECT *,
                           row_number() OVER (
                               PARTITION BY document_id
                               ORDER BY metadata_score DESC, seq
                           ) AS doc_rank
                    FROM candidates
                )
                SELECT chunk_id, seq, content, loc, document_id, filename, source_path,
                       meta, collection, metadata_score
                FROM ranked
                WHERE doc_rank <= 2
                ORDER BY metadata_score DESC, filename, doc_rank
                LIMIT %s""",
            (
                terms,
                [str(c["id"]) for c in cols],
                *params,
                limit,
            ),
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
        **({"metadata_score": float(r["metadata_score"])} if "metadata_score" in r else {}),
        **({"decomposition_query": r["decomposition_query"]} if "decomposition_query" in r else {}),
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
    explicit_top_k = top_k is not None
    top_k = int(top_k or scfg["top_k"])
    top_k = max(1, min(top_k, scfg["max_top_k"]))
    min_score = float(min_score if min_score is not None else scfg["min_score"])
    candidate_k = top_k
    adaptive_return = bool(scfg.get("adaptive_top_k_enabled", False)) and (
        not explicit_top_k or bool(scfg.get("adaptive_top_k_allow_below_explicit", True))
    )
    if rcfg.get("enabled"):
        max_candidates = max(1, int(rcfg.get("max_candidates", 32)))
        if adaptive_return:
            candidate_k = min(
                max_candidates,
                max(
                    int(scfg.get("adaptive_top_k_max", 8)),
                    int(scfg.get("adaptive_top_k_min", 3)),
                ),
            )
        else:
            candidate_k = max(top_k, min(
                max_candidates,
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
        query_intent = analyze_query_intent(query, scfg)
        decomposition = decompose_query(query, scfg)
        hybrid = False
        lexical_skipped_reason = None
        adaptive = bool(scfg.get("adaptive_enabled")) and bool(rcfg.get("enabled"))
        expanded = False
        async def execute_search_pipeline():
            nonlocal embedding_ms, db_ms, results, expanded, dense_ms, lexical_ms, heading_ms, hybrid, lexical_skipped_reason
            dense = []
            lexical = []
            metadata = []
            decomposed_rankings: list[list[dict]] = []
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

            if decomposition.get("enabled"):
                per_query = max(1, int(scfg.get("decomposition_candidates_per_query", candidate_k)))

                async def retrieve_subquery(subquery: str) -> tuple[list[dict], float, float]:
                    ranking: list[dict] = []
                    sub_embedding_ms = 0.0
                    sub_db_ms = 0.0
                    for (model, dim), group_cols in groups.items():
                        rows, emb_elapsed, sub_db_elapsed = await _search_group(
                            query=subquery, model=model, dim=dim, group_cols=group_cols,
                            filters=filters, top_k=per_query, scfg=scfg, client=client,
                        )
                        sub_embedding_ms += emb_elapsed
                        sub_db_ms += sub_db_elapsed
                        ranking.extend(_format_result(r) for r in rows if float(r["score"]) >= min_score)
                    ranking.sort(key=lambda x: x["score"], reverse=True)
                    for item in ranking:
                        item["decomposition_query"] = subquery
                    return ranking, sub_embedding_ms, sub_db_ms

                subquery_results = list(await asyncio.gather(
                    *(retrieve_subquery(sq) for sq in decomposition["subqueries"])
                ))
                decomposed_rankings = [item[0] for item in subquery_results]
                embedding_ms += sum(item[1] for item in subquery_results)
                dense_ms += sum(item[2] for item in subquery_results)
                db_ms += sum(item[2] for item in subquery_results)

            use_lexical = False
            if hybrid_requested:
                use_lexical, lexical_skipped_reason = should_apply_lexical(
                    query, dense, candidate_k, scfg, intent=query_intent
                )
            use_metadata = bool(
                hybrid_requested
                and query_intent.get("metadata_terms")
                and query_intent.get("force_hybrid")
            )
            hybrid = bool(use_lexical or use_metadata)
            rankings: list[list[dict]] = [dense]
            if hybrid:
                t = time.perf_counter()
                if use_lexical:
                    lexical_query = (
                        " ".join(query_intent["lexical_terms"])
                        if lexical_skipped_reason == "query_exact_terms"
                        else query
                    )
                    lexical = [_format_result(r) for r in await _lexical_search(
                        lexical_query, cols, filters, int(scfg["lexical_candidates"]))]
                if use_metadata:
                    metadata = [_format_result(r) for r in await _metadata_search(
                        query_intent["metadata_terms"],
                        cols,
                        filters,
                        int(scfg.get("hybrid_metadata_candidates", 12)),
                    )]
                lexical_ms += (time.perf_counter()-t)*1000
                db_ms += lexical_ms
                rankings.extend([lexical, metadata])
            rankings.extend(decomposed_rankings)
            rankings = [ranking for ranking in rankings if ranking]
            results = (
                fuse_rankings(rankings, int(scfg["fusion_rank_constant"]))
                if len(rankings) > 1
                else (rankings[0] if rankings else [])
            )
            for retrieval_rank, item in enumerate(results, 1):
                item["retrieval_rank"] = retrieval_rank
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

        await runtime_scheduler.ensure_resident(client, requirements, exact=True)
        ranked_results, rerank_pool, rerank_ms, rerank_error = await execute_search_pipeline()
        effective_top_k, adaptive_top_k = choose_adaptive_top_k(
            ranked_results, top_k, scfg, explicit=explicit_top_k
        )
        final_results = ranked_results[:effective_top_k]
        annotate_selection(final_results, hybrid=hybrid, expanded=expanded)
        usefulness = retrieval_usefulness(final_results, scfg)
        cost = token_cost_estimate(query, rerank_pool, final_results, rcfg)
        response = {
            "query": query,
            "results": final_results,
            "collections_searched": [c["name"] for c in cols],
            "filters_applied": filters.to_dict(),
            "total_candidates": len(results),
            "retrieval": {
                "hybrid_requested": hybrid_requested, "hybrid": hybrid,
                "lexical_skipped_reason": lexical_skipped_reason,
                "query_intent": query_intent,
                "decomposition": decomposition,
                "expanded": expanded,
                "requested_top_k": top_k, "effective_top_k": effective_top_k,
                "adaptive_top_k": adaptive_top_k,
                "usefulness": usefulness,
                "token_cost": cost,
                "timings_ms": {"dense": round(dense_ms, 2), "lexical": round(lexical_ms, 2),
                               "heading": round(heading_ms, 2), "embedding": round(embedding_ms, 2)}
            },
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
            "usefulness_score": usefulness["score"],
            "effective_top_k": effective_top_k,
            "estimated_model_input_tokens": cost["total_model_input_tokens"],
            "returned_context_tokens": cost["returned_context_tokens"],
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
