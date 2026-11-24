"""Core collection topology and book categories.

Collections are storage/search scopes. Categories classify documents *inside*
Computer Books. This keeps the top-level knowledge topology intentionally small.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from .db import db
from .settings import settings

COMPUTER_BOOKS = "Computer Books"
KNOWLEDGE_BASE = "Knowledge Base"
RAG_SOURCE_CODE = "RAG Source Code"
CORE_COLLECTIONS = (COMPUTER_BOOKS, KNOWLEDGE_BASE, RAG_SOURCE_CODE)


async def ensure_core_collections(database=None, settings_obj=None) -> dict[str, Any]:
    database = database or db
    settings_obj = settings_obj or settings
    eff = await settings_obj.effective()
    ingest_cfg = dict(eff["ingest"])
    model = await settings_obj.get("embedding_model")
    if model:
        ingest_cfg["embedding_model"] = model

    staging_name = str(
        (eff.get("classification") or {}).get("staging_collection_name") or "Unclassified"
    ).strip()
    specs = {
        COMPUTER_BOOKS: ("Computer and software engineering books.", True),
        KNOWLEDGE_BASE: ("Andre private knowledge base.", False),
        RAG_SOURCE_CODE: ("Searchable source code for this RAG repository.", False),
    }

    async with database.pool.connection() as conn:
        async with conn.transaction():
            core: dict[str, dict] = {}
            for name, (description, default_search) in specs.items():
                cur = await conn.execute(
                    """INSERT INTO collections(
                           id,name,description,ingest_config,default_search,embedding_model)
                       VALUES (%s,%s,%s,%s::jsonb,%s,%s)
                       ON CONFLICT (name) DO UPDATE SET
                         description=EXCLUDED.description,
                         default_search=EXCLUDED.default_search,
                         updated_at=now()
                       RETURNING *""",
                    (
                        str(uuid.uuid4()), name, description,
                        json.dumps(ingest_cfg), default_search, model,
                    ),
                )
                core[name] = await cur.fetchone()

            books = core[COMPUTER_BOOKS]
            cur = await conn.execute(
                """SELECT id,name,description FROM collections
                   WHERE name <> ALL(%s::text[]) ORDER BY name""",
                (list(CORE_COLLECTIONS),),
            )
            legacy = await cur.fetchall()

            migrated_categories = 0
            migrated_documents = 0
            removed_duplicates = 0
            for old in legacy:
                category = None
                if old["name"] != staging_name:
                    cur = await conn.execute(
                        """INSERT INTO categories(id,collection_id,name,description)
                           VALUES (%s,%s,%s,%s)
                           ON CONFLICT (collection_id,name) DO UPDATE SET
                             description=CASE
                               WHEN categories.description='' THEN EXCLUDED.description
                               ELSE categories.description
                             END,
                             updated_at=now()
                           RETURNING id""",
                        (str(uuid.uuid4()), books["id"], old["name"], old["description"] or ""),
                    )
                    category = await cur.fetchone()
                    migrated_categories += 1

                cur = await conn.execute(
                    "SELECT id,content_sha256 FROM documents WHERE collection_id=%s ORDER BY created_at",
                    (old["id"],),
                )
                for doc in await cur.fetchall():
                    duplicate = await (
                        await conn.execute(
                            """SELECT id FROM documents
                               WHERE collection_id=%s AND content_sha256=%s AND id<>%s""",
                            (books["id"], doc["content_sha256"], doc["id"]),
                        )
                    ).fetchone()
                    if duplicate:
                        await conn.execute("DELETE FROM documents WHERE id=%s", (doc["id"],))
                        removed_duplicates += 1
                        continue
                    await conn.execute(
                        "UPDATE documents SET collection_id=%s,category_id=%s,updated_at=now() WHERE id=%s",
                        (books["id"], category["id"] if category else None, doc["id"]),
                    )
                    await conn.execute(
                        "UPDATE chunks SET collection_id=%s WHERE document_id=%s",
                        (books["id"], doc["id"]),
                    )
                    migrated_documents += 1

                # Old directory sources are intentionally not migrated as book categories.
                await conn.execute("DELETE FROM collections WHERE id=%s", (old["id"],))

    return {
        "collections": core,
        "migrated_categories": migrated_categories,
        "migrated_documents": migrated_documents,
        "removed_duplicates": removed_duplicates,
    }


async def computer_books_collection(database=None, settings_obj=None) -> dict[str, Any]:
    database = database or db
    settings_obj = settings_obj or settings
    async with database.pool.connection() as conn:
        row = await (
            await conn.execute("SELECT * FROM collections WHERE name=%s", (COMPUTER_BOOKS,))
        ).fetchone()
    if row:
        return row
    result = await ensure_core_collections(database, settings_obj)
    return result["collections"][COMPUTER_BOOKS]
