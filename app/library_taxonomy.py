"""Core collection topology and book categories.

Collections are stable storage/search scopes. Categories classify documents
inside the book library and are never inferred from legacy collection names.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from .db import db
from .settings import settings

BOOK_LIBRARY = "Informática"
# Compatibility alias for callers; value intentionally points to the new canonical name.
COMPUTER_BOOKS = BOOK_LIBRARY
LEGACY_BOOK_LIBRARY = "Computer Books"
KNOWLEDGE_BASE = "Knowledge Base"
RAG_SOURCE_CODE = "RAG Source Code"
CORE_COLLECTIONS = (BOOK_LIBRARY, KNOWLEDGE_BASE, RAG_SOURCE_CODE)
TAXONOMY_VERSION = 2
INITIAL_CATEGORIES = (
    ("Software Engineering", "Software design, architecture, testing, APIs, patterns, and development practices."),
    ("Data & Databases", "Databases, data engineering, storage, SQL, and data-intensive systems."),
    ("AI & RAG", "Artificial intelligence, LLMs, RAG, agents, embeddings, and related systems."),
)


async def _taxonomy_version(conn) -> int:
    row = await (
        await conn.execute(
            "SELECT value FROM settings WHERE key='library_taxonomy_version'"
        )
    ).fetchone()
    if not row:
        return 0
    value = row["value"]
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def _merge_collection_into(conn, source_id, target_id) -> tuple[int, int]:
    """Move documents to target without carrying old taxonomy semantics."""
    moved = 0
    duplicates = 0
    rows = await (
        await conn.execute(
            "SELECT id,content_sha256 FROM documents WHERE collection_id=%s ORDER BY created_at",
            (source_id,),
        )
    ).fetchall()
    for doc in rows:
        duplicate = await (
            await conn.execute(
                """SELECT id FROM documents
                   WHERE collection_id=%s AND content_sha256=%s AND id<>%s""",
                (target_id, doc["content_sha256"], doc["id"]),
            )
        ).fetchone()
        if duplicate:
            await conn.execute("DELETE FROM documents WHERE id=%s", (doc["id"],))
            duplicates += 1
            continue
        await conn.execute(
            """UPDATE documents
               SET collection_id=%s,category_id=NULL,updated_at=now()
               WHERE id=%s""",
            (target_id, doc["id"]),
        )
        await conn.execute(
            "UPDATE chunks SET collection_id=%s WHERE document_id=%s",
            (target_id, doc["id"]),
        )
        moved += 1
    return moved, duplicates


async def ensure_core_collections(
    database=None, settings_obj=None, *, apply_migration: bool = False
) -> dict[str, Any]:
    database = database or db
    settings_obj = settings_obj or settings
    eff = await settings_obj.effective()
    ingest_cfg = dict(eff["ingest"])
    model = await settings_obj.get("embedding_model")
    if model:
        ingest_cfg["embedding_model"] = model

    specs = {
        BOOK_LIBRARY: ("Technical books about software, data, systems, and AI.", True),
        KNOWLEDGE_BASE: ("Andre private knowledge base.", False),
        RAG_SOURCE_CODE: ("Searchable source code for this RAG repository.", False),
    }

    async with database.pool.connection() as conn:
        async with conn.transaction():
            # Repair the bad v1 deployment in-place: the book collection may already
            # exist under the temporary name "Computer Books".
            book = await (
                await conn.execute(
                    "SELECT * FROM collections WHERE name=%s", (BOOK_LIBRARY,)
                )
            ).fetchone()
            legacy_book = await (
                await conn.execute(
                    "SELECT * FROM collections WHERE name=%s", (LEGACY_BOOK_LIBRARY,)
                )
            ).fetchone()
            if apply_migration and not book and legacy_book:
                book = await (
                    await conn.execute(
                        """UPDATE collections SET name=%s,updated_at=now()
                           WHERE id=%s RETURNING *""",
                        (BOOK_LIBRARY, legacy_book["id"]),
                    )
                ).fetchone()
                legacy_book = None

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

            books = core[BOOK_LIBRARY]
            migrated_documents = 0
            removed_duplicates = 0

            taxonomy_reset = False
            version = await _taxonomy_version(conn)
            if apply_migration:
                # If both names existed, consolidate the temporary one without using
                # its name as a category.
                if legacy_book and legacy_book["id"] != books["id"]:
                    moved, dupes = await _merge_collection_into(
                        conn, legacy_book["id"], books["id"]
                    )
                    migrated_documents += moved
                    removed_duplicates += dupes
                    await conn.execute(
                        "DELETE FROM collections WHERE id=%s", (legacy_book["id"],)
                    )

                # Consolidate any pre-v2 technical shelves into the book library.
                # Their names are legacy storage layout, NOT category definitions.
                cur = await conn.execute(
                    """SELECT id,name FROM collections
                       WHERE name <> ALL(%s::text[]) ORDER BY name""",
                    (list(CORE_COLLECTIONS),),
                )
                for old in await cur.fetchall():
                    moved, dupes = await _merge_collection_into(
                        conn, old["id"], books["id"]
                    )
                    migrated_documents += moved
                    removed_duplicates += dupes
                    await conn.execute("DELETE FROM collections WHERE id=%s", (old["id"],))

            if apply_migration and version < TAXONOMY_VERSION:
                # One-time repair/migration: erase inferred taxonomy and start from
                # an explicit small catalog. Knowledge Base and source code are untouched.
                await conn.execute(
                    "UPDATE documents SET category_id=NULL WHERE collection_id=%s",
                    (books["id"],),
                )
                await conn.execute(
                    "DELETE FROM categories WHERE collection_id=%s", (books["id"],)
                )
                for name, description in INITIAL_CATEGORIES:
                    await conn.execute(
                        """INSERT INTO categories(id,collection_id,name,description)
                           VALUES (%s,%s,%s,%s)""",
                        (str(uuid.uuid4()), books["id"], name, description),
                    )
                await conn.execute(
                    """INSERT INTO settings(key,value,updated_at)
                       VALUES ('library_taxonomy_version',%s::jsonb,now())
                       ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()""",
                    (json.dumps(TAXONOMY_VERSION),),
                )
                taxonomy_reset = True

    return {
        "collections": core,
        "migrated_documents": migrated_documents,
        "removed_duplicates": removed_duplicates,
        "taxonomy_reset": taxonomy_reset,
        "taxonomy_version": TAXONOMY_VERSION,
    }


async def computer_books_collection(database=None, settings_obj=None) -> dict[str, Any]:
    """Compatibility name: return the canonical book library."""
    database = database or db
    settings_obj = settings_obj or settings
    async with database.pool.connection() as conn:
        row = await (
            await conn.execute("SELECT * FROM collections WHERE name=%s", (BOOK_LIBRARY,))
        ).fetchone()
    if row:
        return row
    result = await ensure_core_collections(database, settings_obj)
    return result["collections"][BOOK_LIBRARY]
