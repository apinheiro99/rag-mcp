#!/usr/bin/env python3
"""Apply the explicit, transactional library taxonomy migration."""
import asyncio
import json

from app.db import db
from app.library_taxonomy import ensure_core_collections
from app.settings import settings




async def refresh_planner_statistics(database) -> None:
    """Refresh PostgreSQL planner statistics after bulk taxonomy updates."""
    async with database.pool.connection() as conn:
        for table in ("collections", "documents", "chunks"):
            await conn.execute(f"ANALYZE {table}")


async def main():
    await db.start()
    try:
        await settings.load()
        result = await ensure_core_collections(db, settings, apply_migration=True)
        await refresh_planner_statistics(db)
        print(json.dumps({
            "taxonomy_version": result["taxonomy_version"],
            "taxonomy_reset": result["taxonomy_reset"],
            "migrated_documents": result["migrated_documents"],
            "removed_duplicates": result["removed_duplicates"],
            "collections": sorted(result["collections"]),
            "planner_statistics_refreshed": True,
        }, ensure_ascii=False))
    finally:
        await db.stop()


if __name__ == "__main__":
    asyncio.run(main())
