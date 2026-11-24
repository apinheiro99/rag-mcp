"""Persistência e leitura de observabilidade das buscas RAG."""
from __future__ import annotations

import json
import logging
from typing import Any

from .db import db

log = logging.getLogger("rag.retrieval_observability")


async def record_retrieval(event: dict[str, Any]) -> int | None:
    try:
        async with db.pool.connection() as conn:
            cur = await conn.execute(
                """INSERT INTO retrieval_events(
                       query_text, collections, filters, top_k, min_score,
                       embedding_ms, db_ms, total_ms, candidate_count,
                       result_count, top_score, error, dense_ms, lexical_ms, heading_ms)
                   VALUES (%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING id""",
                (
                    event["query"],
                    json.dumps(event.get("collections", [])),
                    json.dumps(event.get("filters", {})),
                    event["top_k"],
                    event["min_score"],
                    event.get("embedding_ms", 0.0),
                    event.get("db_ms", 0.0),
                    event.get("total_ms", 0.0),
                    event.get("candidate_count", 0),
                    event.get("result_count", 0),
                    event.get("top_score"),
                    event.get("error"),
                    event.get("dense_ms"), event.get("lexical_ms"), event.get("heading_ms"),
                ),
            )
            row = await cur.fetchone()
            return int(row["id"]) if row else None
    except Exception:
        log.exception("falha ao registrar observabilidade de retrieval")
        return None


async def retrieval_metrics(hours: int = 24, recent_limit: int = 20) -> dict[str, Any]:
    hours = max(1, min(int(hours), 24 * 90))
    recent_limit = max(1, min(int(recent_limit), 100))
    async with db.pool.connection() as conn:
        summary = await (
            await conn.execute(
                """SELECT count(*) AS queries,
                          count(*) FILTER (WHERE error IS NOT NULL) AS errors,
                          count(*) FILTER (WHERE result_count=0 AND error IS NULL) AS zero_results,
                          round(avg(total_ms)::numeric,2) AS avg_total_ms,
                          round(avg(embedding_ms)::numeric,2) AS avg_embedding_ms,
                          round(avg(db_ms)::numeric,2) AS avg_db_ms,
                          round(avg(dense_ms)::numeric,2) AS avg_dense_ms,
                          round(avg(lexical_ms)::numeric,2) AS avg_lexical_ms,
                          round(avg(heading_ms)::numeric,2) AS avg_heading_ms,
                          round(avg(result_count)::numeric,2) AS avg_results,
                          round(avg(top_score)::numeric,4) AS avg_top_score,
                          round(percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms)::numeric,2) AS p95_total_ms
                   FROM retrieval_events
                   WHERE ts >= now() - (%s * interval '1 hour')""",
                (hours,),
            )
        ).fetchone()
        recent = await (
            await conn.execute(
                """SELECT id, ts, query_text, collections, filters, top_k, min_score,
                          embedding_ms, db_ms, total_ms, candidate_count,
                          result_count, top_score, error, dense_ms, lexical_ms, heading_ms
                   FROM retrieval_events
                   ORDER BY id DESC LIMIT %s""",
                (recent_limit,),
            )
        ).fetchall()
    return {"window_hours": hours, "summary": summary, "recent": recent}
