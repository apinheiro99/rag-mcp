"""Acesso ao PostgreSQL/pgvector: pool, schema, tabelas de vetores por dimensão,
settings persistidos e log de eventos."""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import env

log = logging.getLogger("rag.db")

MAX_HNSW_VECTOR_DIM = 2000
MAX_HNSW_HALFVEC_DIM = 4000


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{float(v):.7g}" for v in values) + "]"


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: AsyncConnectionPool | None = None

    async def start(self) -> None:
        self.pool = AsyncConnectionPool(
            self.dsn,
            min_size=2,
            max_size=10,
            kwargs={"row_factory": dict_row, "autocommit": True},
            open=False,
            name="rag-pool",
        )
        await self.pool.open(wait=True, timeout=60)
        await self.apply_schema()
        log.info("database ready")

    async def stop(self) -> None:
        if self.pool:
            await self.pool.close()

    async def apply_schema(self) -> None:
        sql = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
        async with self.pool.connection() as conn:
            await conn.execute(sql)

    # ---------- settings ----------
    async def get_setting(self, key: str, default: Any = None) -> Any:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT value FROM settings WHERE key=%s", (key,))
            row = await cur.fetchone()
            return row["value"] if row else default

    async def set_setting(self, key: str, value: Any) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO settings(key, value, updated_at) VALUES (%s, %s::jsonb, now())
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()""",
                (key, json.dumps(value)),
            )

    # ---------- eventos ----------
    async def event(self, message: str, level: str = "info",
                    collection_id: uuid.UUID | str | None = None,
                    document_id: uuid.UUID | str | None = None,
                    keep: int = 5000) -> None:
        try:
            async with self.pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO events(level, collection_id, document_id, message) VALUES (%s,%s,%s,%s)",
                    (level, str(collection_id) if collection_id else None,
                     str(document_id) if document_id else None, message),
                )
                await conn.execute(
                    "DELETE FROM events WHERE id <= (SELECT max(id) FROM events) - %s", (keep,)
                )
        except Exception:  # log de eventos nunca pode derrubar o pipeline
            log.exception("failed to record event")

    # ---------- vetores por dimensão ----------
    async def ensure_vector_table(self, dim: int) -> tuple[str, str]:
        """Cria (idempotente) a tabela de vetores para `dim`.

        Retorna (nome_tabela, tipo_coluna). vector até 2000 dims com HNSW;
        halfvec até 4000 dims com HNSW; acima disso vector sem índice.
        """
        if dim <= 0 or dim > 16000:
            raise ValueError(f"dimensão de embedding inválida: {dim}")
        table = f"vec_{dim}"
        if dim <= MAX_HNSW_VECTOR_DIM:
            coltype = "vector"
        elif dim <= MAX_HNSW_HALFVEC_DIM:
            coltype = "halfvec"
        else:
            coltype = "vector"
        async with self.pool.connection() as conn:
            await conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {table} (
                        chunk_id UUID PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
                        embedding {coltype}({dim}) NOT NULL)"""
            )
            if dim <= MAX_HNSW_HALFVEC_DIM:
                await conn.execute(
                    f"""CREATE INDEX IF NOT EXISTS {table}_hnsw
                        ON {table} USING hnsw (embedding {coltype}_cosine_ops)"""
                )
        return table, coltype

    async def vector_table_for_dim(self, dim: int) -> tuple[str, str] | None:
        table = f"vec_{dim}"
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT format_type(a.atttypid, a.atttypmod) AS t
                   FROM pg_attribute a
                   WHERE a.attrelid = to_regclass(%s) AND a.attname='embedding'""",
                (table,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            coltype = "halfvec" if row["t"].startswith("halfvec") else "vector"
            return table, coltype


db = Database(env.dsn)
