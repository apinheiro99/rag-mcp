#!/usr/bin/env python3
"""Apply the explicit, transactional library taxonomy migration."""
import asyncio
import json

from app.db import db
from app.library_taxonomy import ensure_core_collections
from app.settings import settings


async def main():
    await db.start()
    try:
        await settings.load()
        result = await ensure_core_collections(db, settings, apply_migration=True)
        print(json.dumps({
            "taxonomy_version": result["taxonomy_version"],
            "taxonomy_reset": result["taxonomy_reset"],
            "migrated_documents": result["migrated_documents"],
            "removed_duplicates": result["removed_duplicates"],
            "collections": sorted(result["collections"]),
        }, ensure_ascii=False))
    finally:
        await db.stop()


if __name__ == "__main__":
    asyncio.run(main())
