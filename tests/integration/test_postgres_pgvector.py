import asyncio
import json
import os
import uuid

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from psycopg import sql

import app.api as api_module
import app.ingest as ingest_module
import app.retrieval_observability as observability_module
import app.search as search_module
import app.source_sync as source_sync_module
from app.db import Database, vector_literal
from app.ingest import DocumentIngestor, IngestStore
from app.model_runtime import runtime_scheduler


pytestmark = pytest.mark.integration
TEST_DB_PREFIX = "rag_test_"


def _connection_kwargs(database: str) -> dict:
    return {
        "host": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "port": int(os.environ.get("POSTGRES_EXPOSE_PORT", os.environ.get("POSTGRES_PORT", "5432"))),
        "dbname": database,
        "user": os.environ.get("POSTGRES_USER", "rag"),
        "password": os.environ["POSTGRES_PASSWORD"],
        "connect_timeout": 5,
    }


def _dsn(database: str) -> str:
    k = _connection_kwargs(database)
    return (
        f"postgresql://{k['user']}:{k['password']}@"
        f"{k['host']}:{k['port']}/{database}"
    )


@pytest.fixture(scope="session")
def test_database_name():
    name = f"{TEST_DB_PREFIX}{uuid.uuid4().hex[:12]}"
    assert name.startswith(TEST_DB_PREFIX)
    assert name != os.environ.get("POSTGRES_DB", "rag")
    return name


@pytest.fixture(scope="session", autouse=True)
def isolated_test_database(test_database_name):
    real_db = os.environ.get("POSTGRES_DB", "rag")
    if test_database_name == real_db or not test_database_name.startswith(TEST_DB_PREFIX):
        pytest.fail("SAFETY: integration tests refused to use the real database")

    admin = psycopg.connect(**_connection_kwargs("postgres"), autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(test_database_name))
            )
    finally:
        admin.close()

    yield

    admin = psycopg.connect(**_connection_kwargs("postgres"), autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (test_database_name,),
            )
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(test_database_name)
                )
            )
    finally:
        admin.close()


@pytest.fixture
async def live_db(test_database_name):
    assert test_database_name.startswith(TEST_DB_PREFIX)
    db = Database(_dsn(test_database_name))
    await db.start()
    try:
        yield db
    finally:
        await runtime_scheduler.stop()
        await db.stop()


@pytest.mark.asyncio
async def test_schema_is_idempotent_and_pgvector_available(live_db):
    await live_db.apply_schema()
    async with live_db.pool.connection() as conn:
        version = await (await conn.execute("SHOW server_version")).fetchone()
        ext = await (
            await conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            )
        ).fetchone()

    assert version["server_version"].startswith("19.")
    assert ext["extversion"] == "0.8.7"


@pytest.mark.asyncio
async def test_collection_document_chunk_and_vector_search(live_db):
    collection_id = uuid.uuid4()
    document_id = uuid.uuid4()
    chunk_near = uuid.uuid4()
    chunk_far = uuid.uuid4()

    async with live_db.pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO collections(id, name, embedding_model, embedding_dim)
            VALUES (%s, %s, %s, %s)
            """,
            (collection_id, f"itest-{collection_id}", "integration-test", 3),
        )
        await conn.execute(
            """
            INSERT INTO documents(
                id, collection_id, filename, source_path, stored_path,
                content_sha256, size_bytes, status
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                document_id,
                collection_id,
                "sample.txt",
                "sample.txt",
                "blobs/sample.txt",
                uuid.uuid4().hex,
                12,
                "ready",
            ),
        )
        await conn.execute(
            """
            INSERT INTO chunks(id, document_id, collection_id, seq, content, embedded)
            VALUES
                (%s,%s,%s,0,'near',TRUE),
                (%s,%s,%s,1,'far',TRUE)
            """,
            (
                chunk_near,
                document_id,
                collection_id,
                chunk_far,
                document_id,
                collection_id,
            ),
        )

    table, coltype = await live_db.ensure_vector_table(3)
    assert table == "vec_3"
    assert coltype == "vector"

    async with live_db.pool.connection() as conn:
        await conn.execute(
            f"INSERT INTO {table}(chunk_id, embedding) "
            "VALUES (%s, %s::vector), (%s, %s::vector)",
            (
                chunk_near,
                vector_literal([1, 0, 0]),
                chunk_far,
                vector_literal([0, 1, 0]),
            ),
        )
        rows = await (
            await conn.execute(
                f"""
                SELECT c.content, v.embedding <=> %s::vector AS distance
                FROM {table} v
                JOIN chunks c ON c.id = v.chunk_id
                WHERE c.collection_id = %s
                ORDER BY distance
                """,
                (vector_literal([1, 0, 0]), collection_id),
            )
        ).fetchall()

    assert [row["content"] for row in rows] == ["near", "far"]
    assert float(rows[0]["distance"]) == pytest.approx(0.0)
    assert float(rows[1]["distance"]) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_persisted_setting_roundtrip(live_db):
    key = f"integration.{uuid.uuid4()}"
    value = {"enabled": True, "limit": 7}
    await live_db.set_setting(key, value)
    assert await live_db.get_setting(key) == value


@pytest.fixture
async def api_client(live_db, monkeypatch):
    monkeypatch.setattr(api_module, "db", live_db)
    app = FastAPI()
    app.include_router(api_module.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_top_level_collection_creation_is_locked(api_client):
    response = await api_client.post("/api/collections", json={"name": "Economics"})
    assert response.status_code == 409
    assert "top-level collection topology is fixed" in response.json()["detail"]


async def _insert_collection(conn, collection_id, name, model="same-model", dim=3):
    await conn.execute(
        """INSERT INTO collections(id, name, embedding_model, embedding_dim)
           VALUES (%s,%s,%s,%s)""",
        (collection_id, name, model, dim),
    )


async def _insert_document(conn, document_id, collection_id, *, status="ready",
                           content_sha256=None, filename="move-me.txt"):
    digest = content_sha256 or uuid.uuid4().hex
    await conn.execute(
        """INSERT INTO documents(
               id, collection_id, filename, source_path, stored_path,
               content_sha256, size_bytes, status, chunks_total, chunks_embedded)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,1)""",
        (
            document_id, collection_id, filename, filename,
            f"blobs/{document_id}", digest, 7, status,
        ),
    )
    return digest


@pytest.mark.asyncio
async def test_move_document_api_updates_document_and_chunks(live_db, api_client):
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()
    document_id = uuid.uuid4()
    chunk_id = uuid.uuid4()

    async with live_db.pool.connection() as conn:
        await _insert_collection(conn, source_id, f"source-{source_id}")
        await _insert_collection(conn, target_id, f"target-{target_id}")
        await _insert_document(conn, document_id, source_id)
        await conn.execute(
            """INSERT INTO chunks(id, document_id, collection_id, seq, content, embedded)
               VALUES (%s,%s,%s,0,'content',TRUE)""",
            (chunk_id, document_id, source_id),
        )

    response = await api_client.post(
        f"/api/documents/{document_id}/move",
        json={"target_collection_id": str(target_id)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["unchanged"] is False
    assert body["to_collection"] == f"target-{target_id}"

    async with live_db.pool.connection() as conn:
        doc_collection = (
            await (
                await conn.execute(
                    "SELECT collection_id FROM documents WHERE id=%s",
                    (document_id,),
                )
            ).fetchone()
        )["collection_id"]
        chunk_collection = (
            await (
                await conn.execute(
                    "SELECT collection_id FROM chunks WHERE id=%s",
                    (chunk_id,),
                )
            ).fetchone()
        )["collection_id"]

    assert doc_collection == target_id
    assert chunk_collection == target_id


@pytest.mark.asyncio
async def test_move_document_api_rejects_incompatible_embedding_collection(live_db, api_client):
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()
    document_id = uuid.uuid4()

    async with live_db.pool.connection() as conn:
        await _insert_collection(conn, source_id, f"source-{source_id}", "model-a", 3)
        await _insert_collection(conn, target_id, f"target-{target_id}", "model-b", 4)
        await _insert_document(conn, document_id, source_id)

    response = await api_client.post(
        f"/api/documents/{document_id}/move",
        json={"target_collection_id": str(target_id)},
    )

    assert response.status_code == 409

    async with live_db.pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT collection_id FROM documents WHERE id=%s",
                (document_id,),
            )
        ).fetchone()

    assert row["collection_id"] == source_id


@pytest.mark.asyncio
async def test_move_document_api_is_idempotent_for_same_collection(live_db, api_client):
    collection_id = uuid.uuid4()
    document_id = uuid.uuid4()

    async with live_db.pool.connection() as conn:
        await _insert_collection(conn, collection_id, f"same-{collection_id}")
        await _insert_document(conn, document_id, collection_id)

    response = await api_client.post(
        f"/api/documents/{document_id}/move",
        json={"target_collection_id": str(collection_id)},
    )

    assert response.status_code == 200
    assert response.json()["unchanged"] is True


@pytest.mark.asyncio
async def test_move_document_api_rejects_active_document(live_db, api_client):
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()
    document_id = uuid.uuid4()

    async with live_db.pool.connection() as conn:
        await _insert_collection(conn, source_id, f"source-{source_id}")
        await _insert_collection(conn, target_id, f"target-{target_id}")
        await _insert_document(conn, document_id, source_id, status="embedding")

    response = await api_client.post(
        f"/api/documents/{document_id}/move",
        json={"target_collection_id": str(target_id)},
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_move_document_api_rejects_duplicate_in_target(live_db, api_client):
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()
    moving_id = uuid.uuid4()
    existing_id = uuid.uuid4()
    digest = uuid.uuid4().hex

    async with live_db.pool.connection() as conn:
        await _insert_collection(conn, source_id, f"source-{source_id}")
        await _insert_collection(conn, target_id, f"target-{target_id}")
        await _insert_document(conn, moving_id, source_id, content_sha256=digest)
        await _insert_document(conn, existing_id, target_id, content_sha256=digest,
                               filename="already-there.txt")

    response = await api_client.post(
        f"/api/documents/{moving_id}/move",
        json={"target_collection_id": str(target_id)},
    )

    assert response.status_code == 409

    async with live_db.pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT collection_id FROM documents WHERE id=%s",
                (moving_id,),
            )
        ).fetchone()

    assert row["collection_id"] == source_id


@pytest.mark.asyncio
async def test_move_document_api_returns_404_for_missing_target(live_db, api_client):
    source_id = uuid.uuid4()
    document_id = uuid.uuid4()

    async with live_db.pool.connection() as conn:
        await _insert_collection(conn, source_id, f"source-{source_id}")
        await _insert_document(conn, document_id, source_id)

    response = await api_client.post(
        f"/api/documents/{document_id}/move",
        json={"target_collection_id": str(uuid.uuid4())},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_upload_to_configured_collection_is_auto_queued(
    live_db, api_client, monkeypatch, tmp_path
):
    collection_id = uuid.uuid4()
    collection_config = {
        "strategy": "recursive",
        "chunk_size": 1500,
        "chunk_overlap": 200,
        "min_chunk_size": 120,
        "embed_batch_size": 16,
    }

    async with live_db.pool.connection() as conn:
        await conn.execute(
            """INSERT INTO collections(
                   id, name, embedding_model, embedding_dim, ingest_config)
               VALUES (%s,%s,%s,%s,%s::jsonb)""",
            (
                collection_id,
                f"configured-{collection_id}",
                "embedding-model",
                3,
                json.dumps(collection_config),
            ),
        )

    class FakeSettings:
        async def effective(self):
            return {
                "allowed_extensions": [".txt"],
                "ingest": {"max_file_mb": 10},
                "workflow": {"auto_start_configured_uploads": True},
            }

        async def get(self, key, default=None):
            return "embedding-model" if key == "embedding_model" else default

    async def fake_resolve(collection_cfg, override):
        assert collection_cfg == collection_config
        assert override is None
        return dict(collection_config)

    monkeypatch.setattr(api_module, "settings", FakeSettings())
    monkeypatch.setattr(api_module, "resolve_ingest_config", fake_resolve)
    monkeypatch.setattr(api_module.env, "data_dir", tmp_path)

    response = await api_client.post(
        "/api/documents/upload",
        data={"collection_id": str(collection_id), "paths": "auto.txt"},
        files={"files": ("auto.txt", b"automatic ingestion regression", "text/plain")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["queued"] == 1
    assert body["staged"] == 0
    assert body["results"][0]["status"] == "pending"

    document_id = body["results"][0]["document_id"]
    async with live_db.pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT status, ingest_config FROM documents WHERE id=%s",
                (document_id,),
            )
        ).fetchone()

    assert row["status"] == "pending"
    assert row["ingest_config"]["embedding_model"] == "embedding-model"


@pytest.mark.asyncio
async def test_upload_to_unconfigured_collection_remains_staged(
    live_db, api_client, monkeypatch, tmp_path
):
    collection_id = uuid.uuid4()

    async with live_db.pool.connection() as conn:
        await _insert_collection(conn, collection_id, f"unconfigured-{collection_id}",
                                 model=None, dim=None)

    class FakeSettings:
        async def effective(self):
            return {
                "allowed_extensions": [".txt"],
                "ingest": {"max_file_mb": 10},
                "workflow": {"auto_start_configured_uploads": True},
            }

        async def get(self, key, default=None):
            return "embedding-default" if key == "embedding_model" else default

    monkeypatch.setattr(api_module, "settings", FakeSettings())
    monkeypatch.setattr(api_module.env, "data_dir", tmp_path)

    response = await api_client.post(
        "/api/documents/upload",
        data={"collection_id": str(collection_id), "paths": "manual.txt"},
        files={"files": ("manual.txt", b"needs configuration", "text/plain")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["queued"] == 0
    assert body["staged"] == 1
    assert body["results"][0]["status"] == "staged"

@pytest.mark.asyncio
async def test_embedding_progress_counts_only_embedded_chunks(live_db, monkeypatch):
    collection_id = uuid.uuid4()
    document_id = uuid.uuid4()
    chunk_ids = [uuid.uuid4() for _ in range(3)]

    monkeypatch.setattr(ingest_module, "db", live_db)
    table, coltype = await live_db.ensure_vector_table(3)

    async with live_db.pool.connection() as conn:
        await _insert_collection(conn, collection_id, f"progress-{collection_id}")
        await conn.execute(
            """INSERT INTO documents(
                   id, collection_id, filename, source_path, stored_path,
                   content_sha256, size_bytes, status, chunks_total, chunks_embedded)
               VALUES (%s,%s,%s,%s,%s,%s,%s,'embedding',3,0)""",
            (
                document_id, collection_id, "progress.txt", "progress.txt",
                f"blobs/{document_id}", uuid.uuid4().hex, 10,
            ),
        )
        for seq, chunk_id in enumerate(chunk_ids):
            await conn.execute(
                """INSERT INTO chunks(
                       id, document_id, collection_id, seq, content, embedded)
                   VALUES (%s,%s,%s,%s,%s,FALSE)""",
                (chunk_id, document_id, collection_id, seq, f"chunk-{seq}"),
            )

    store = IngestStore()
    doc = {"id": document_id, "collection_id": collection_id}
    collection = {
        "embedding_model": "same-model",
        "embedding_dim": 3,
    }
    batch = [{"id": chunk_ids[0], "content": "chunk-0"}]

    await store.persist_embeddings(
        doc, collection, "same-model", 3, table, coltype, batch, [[1.0, 0.0, 0.0]]
    )

    async with live_db.pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT chunks_embedded FROM documents WHERE id=%s",
                (document_id,),
            )
        ).fetchone()
        actual = await (
            await conn.execute(
                """SELECT count(*) AS n FROM chunks
                   WHERE document_id=%s AND embedded=TRUE""",
                (document_id,),
            )
        ).fetchone()

    assert row["chunks_embedded"] == 1
    assert actual["n"] == 1


class _FakeEmbeddingClient:
    def __init__(self):
        self.loaded = {"integration-test"}

    async def embed(self, texts, model):
        return [[1.0, 0.0, 0.0] for _ in texts]

    async def list_models(self):
        return [{"id": m, "state": "loaded"} for m in sorted(self.loaded)]

    async def unload_model(self, model):
        self.loaded.discard(model)
        return {"status": "ok"}

    async def ensure_model_loaded(self, model, *, context_length=None):
        self.loaded.add(model)
        return {"instance_id": model}


class _FakeSearchSettings:
    async def effective(self):
        return {
            "search": {
                "top_k": 8,
                "max_top_k": 50,
                "min_score": 0.0,
                "hnsw_ef_search": 64,
            }
        }

    async def lmstudio_client(self):
        return _FakeEmbeddingClient()

    async def get(self, key, default=None):
        return default


@pytest.mark.asyncio
async def test_search_api_filters_by_document_and_records_observability(
    live_db, api_client, monkeypatch
):
    collection_id = uuid.uuid4()
    doc_a = uuid.uuid4()
    doc_b = uuid.uuid4()
    chunk_a = uuid.uuid4()
    chunk_b = uuid.uuid4()

    monkeypatch.setattr(search_module, "db", live_db)
    monkeypatch.setattr(search_module, "settings", _FakeSearchSettings())
    monkeypatch.setattr(observability_module, "db", live_db)

    async with live_db.pool.connection() as conn:
        await _insert_collection(conn, collection_id, f"search-{collection_id}",
                                 model="integration-test", dim=3)
        await conn.execute(
            """INSERT INTO documents(
                   id, collection_id, filename, source_path, stored_path,
                   content_sha256, size_bytes, status, meta)
               VALUES
                   (%s,%s,'a.txt','a.txt','blobs/a',%s,10,'ready',%s::jsonb),
                   (%s,%s,'b.txt','b.txt','blobs/b',%s,10,'ready',%s::jsonb)""",
            (
                doc_a, collection_id, uuid.uuid4().hex, json.dumps({"author": "Alice"}),
                doc_b, collection_id, uuid.uuid4().hex, json.dumps({"author": "Bob"}),
            ),
        )
        await conn.execute(
            """INSERT INTO chunks(id, document_id, collection_id, seq, content, loc, embedded)
               VALUES
                   (%s,%s,%s,0,'alpha result',%s::jsonb,TRUE),
                   (%s,%s,%s,0,'beta result',%s::jsonb,TRUE)""",
            (
                chunk_a, doc_a, collection_id, json.dumps({"chapter": "One", "page": 7}),
                chunk_b, doc_b, collection_id, json.dumps({"chapter": "Two", "page": 9}),
            ),
        )

    table, _ = await live_db.ensure_vector_table(3)
    async with live_db.pool.connection() as conn:
        await conn.execute(
            f"""INSERT INTO {table}(chunk_id, embedding)
                VALUES (%s, %s::vector), (%s, %s::vector)""",
            (
                chunk_a, vector_literal([1.0, 0.0, 0.0]),
                chunk_b, vector_literal([0.99, 0.01, 0.0]),
            ),
        )

    response = await api_client.post(
        "/api/search",
        json={
            "query": "find result",
            "document_ids": [str(doc_b)],
            "top_k": 5,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["filters_applied"]["document_ids"] == [str(doc_b)]
    assert body["results"]
    assert {row["document_id"] for row in body["results"]} == {str(doc_b)}

    metrics = await api_client.get("/api/search/metrics?hours=24&recent_limit=5")
    assert metrics.status_code == 200
    mbody = metrics.json()
    assert mbody["summary"]["queries"] >= 1
    assert mbody["recent"][0]["filters"]["document_ids"] == [str(doc_b)]
    assert mbody["recent"][0]["result_count"] >= 1
    assert mbody["recent"][0]["total_ms"] >= 0


@pytest.mark.asyncio
async def test_search_api_supports_author_chapter_and_page_filters(
    live_db, api_client, monkeypatch
):
    collection_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    chunk_match = uuid.uuid4()
    chunk_other = uuid.uuid4()

    monkeypatch.setattr(search_module, "db", live_db)
    monkeypatch.setattr(search_module, "settings", _FakeSearchSettings())
    monkeypatch.setattr(observability_module, "db", live_db)

    async with live_db.pool.connection() as conn:
        await _insert_collection(conn, collection_id, f"filters-{collection_id}",
                                 model="integration-test", dim=3)
        await conn.execute(
            """INSERT INTO documents(
                   id, collection_id, filename, source_path, stored_path,
                   content_sha256, size_bytes, status, meta)
               VALUES (%s,%s,'book.epub','book.epub','blobs/book',%s,10,'ready',%s::jsonb)""",
            (
                doc_id, collection_id, uuid.uuid4().hex,
                json.dumps({"author": "Martin Fowler"}),
            ),
        )
        await conn.execute(
            """INSERT INTO chunks(id, document_id, collection_id, seq, content, loc, embedded)
               VALUES
                   (%s,%s,%s,0,'matching chapter',%s::jsonb,TRUE),
                   (%s,%s,%s,1,'other chapter',%s::jsonb,TRUE)""",
            (
                chunk_match, doc_id, collection_id,
                json.dumps({"chapter": "Refactoring", "page": 12}),
                chunk_other, doc_id, collection_id,
                json.dumps({"chapter": "Other", "page": 13}),
            ),
        )

    table, _ = await live_db.ensure_vector_table(3)
    async with live_db.pool.connection() as conn:
        await conn.execute(
            f"""INSERT INTO {table}(chunk_id, embedding)
                VALUES (%s, %s::vector), (%s, %s::vector)""",
            (
                chunk_match, vector_literal([1.0, 0.0, 0.0]),
                chunk_other, vector_literal([1.0, 0.0, 0.0]),
            ),
        )

    response = await api_client.post(
        "/api/search",
        json={
            "query": "refactoring",
            "authors": ["Fowler"],
            "source_paths": ["book.epub"],
            "chapters": ["Refactoring"],
            "pages": [12],
            "top_k": 5,
        },
    )

    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 1
    assert results[0]["chunk_id"] == str(chunk_match)


@pytest.mark.asyncio
async def test_search_without_filters_keeps_existing_behavior(
    live_db, api_client, monkeypatch
):
    collection_id = uuid.uuid4()
    doc_a = uuid.uuid4()
    doc_b = uuid.uuid4()
    chunk_a = uuid.uuid4()
    chunk_b = uuid.uuid4()

    monkeypatch.setattr(search_module, "db", live_db)
    monkeypatch.setattr(search_module, "settings", _FakeSearchSettings())
    monkeypatch.setattr(observability_module, "db", live_db)

    async with live_db.pool.connection() as conn:
        await _insert_collection(conn, collection_id, f"regression-{collection_id}",
                                 model="integration-test", dim=3)
        for doc_id, filename in ((doc_a, "a.txt"), (doc_b, "b.txt")):
            await conn.execute(
                """INSERT INTO documents(
                       id, collection_id, filename, source_path, stored_path,
                       content_sha256, size_bytes, status)
                   VALUES (%s,%s,%s,%s,%s,%s,10,'ready')""",
                (
                    doc_id, collection_id, filename, filename,
                    f"blobs/{doc_id}", uuid.uuid4().hex,
                ),
            )
        await conn.execute(
            """INSERT INTO chunks(id, document_id, collection_id, seq, content, embedded)
               VALUES
                   (%s,%s,%s,0,'first',TRUE),
                   (%s,%s,%s,0,'second',TRUE)""",
            (chunk_a, doc_a, collection_id, chunk_b, doc_b, collection_id),
        )

    table, _ = await live_db.ensure_vector_table(3)
    async with live_db.pool.connection() as conn:
        await conn.execute(
            f"""INSERT INTO {table}(chunk_id, embedding)
                VALUES (%s, %s::vector), (%s, %s::vector)""",
            (
                chunk_a, vector_literal([1.0, 0.0, 0.0]),
                chunk_b, vector_literal([0.9, 0.1, 0.0]),
            ),
        )

    response = await api_client.post(
        "/api/search",
        json={"query": "anything", "collections": [f"regression-{collection_id}"], "top_k": 2},
    )

    assert response.status_code == 200
    ids = {row["document_id"] for row in response.json()["results"]}
    assert ids == {str(doc_a), str(doc_b)}


class _FakeIngestSettings:
    async def effective(self):
        return {
            "ingest": {
                "strip_whitespace": True,
                "retry_backoff_base": 1,
                "retry_backoff_max": 5,
            },
            "quality": {
                "enabled": True,
                "fail_on_bad": True,
                "min_text_chars": 50,
                "max_control_char_ratio": 0.01,
                "max_replacement_char_ratio": 0.002,
                "max_mojibake_marker_ratio": 0.003,
                "warning_ratio_factor": 0.5,
            },
            "worker": {"event_log_keep": 100},
        }

    async def get(self, key, default=None):
        return default


@pytest.mark.asyncio
async def test_ingestion_quality_gate_rejects_bad_text_before_chunking(
    live_db, monkeypatch, tmp_path
):
    collection_id = uuid.uuid4()
    document_id = uuid.uuid4()
    bad_text = ("normal words " * 30) + ("\x03garbage\x14" * 40)
    (tmp_path / "bad.txt").write_text(bad_text, encoding="utf-8")

    monkeypatch.setattr(ingest_module, "db", live_db)
    monkeypatch.setattr(ingest_module, "settings", _FakeIngestSettings())
    monkeypatch.setattr(ingest_module.env, "data_dir", tmp_path)

    config = {
        "strategy": "recursive",
        "chunk_size": 500,
        "chunk_overlap": 50,
        "min_chunk_size": 50,
        "separators": ["\n\n", "\n", ". ", " "],
        "embedding_model": "integration-test",
        "embed_batch_size": 4,
    }

    async with live_db.pool.connection() as conn:
        await _insert_collection(conn, collection_id, f"quality-{collection_id}",
                                 model="integration-test", dim=3)
        await conn.execute(
            """INSERT INTO documents(
                   id, collection_id, filename, source_path, stored_path,
                   content_sha256, size_bytes, status, ingest_config)
               VALUES (%s,%s,'bad.txt','bad.txt','bad.txt',%s,%s,'pending',%s::jsonb)""",
            (
                document_id, collection_id, uuid.uuid4().hex,
                len(bad_text), json.dumps(config),
            ),
        )

    store = IngestStore()
    async with live_db.pool.connection() as conn:
        doc = await (
            await conn.execute(
                "SELECT * FROM documents WHERE id=%s",
                (document_id,),
            )
        ).fetchone()
    ingestor = DocumentIngestor(store, asyncio.Event())
    await ingestor.process(doc)

    async with live_db.pool.connection() as conn:
        row = await (
            await conn.execute(
                """SELECT status, error, chunks_total, meta
                   FROM documents WHERE id=%s""",
                (document_id,),
            )
        ).fetchone()
        chunk_count = await (
            await conn.execute(
                "SELECT count(*) AS n FROM chunks WHERE document_id=%s",
                (document_id,),
            )
        ).fetchone()

    assert row["status"] == "error"
    assert row["error"].startswith("Qualidade:")
    assert row["meta"]["quality"]["status"] == "bad"
    assert row["chunks_total"] == 0
    assert chunk_count["n"] == 0


@pytest.mark.asyncio
async def test_cancel_pending_document_is_immediate_and_idempotent(live_db, api_client):
    collection_id = uuid.uuid4()
    document_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    async with live_db.pool.connection() as conn:
        await _insert_collection(conn, collection_id, f"cancel-{collection_id}")
        await _insert_document(conn, document_id, collection_id, status="pending")
        await conn.execute(
            """INSERT INTO chunks(id, document_id, collection_id, seq, content, embedded)
               VALUES (%s,%s,%s,0,'partial',FALSE)""",
            (chunk_id, document_id, collection_id),
        )

    first = await api_client.post(f"/api/documents/{document_id}/cancel")
    second = await api_client.post(f"/api/documents/{document_id}/cancel")

    assert first.status_code == 200
    assert first.json()["status"] == "cancelled"
    assert second.status_code == 200
    assert second.json()["status"] == "cancelled"
    assert second.json()["unchanged"] is True

    async with live_db.pool.connection() as conn:
        doc = await (await conn.execute(
            "SELECT status, chunks_total, chunks_embedded FROM documents WHERE id=%s",
            (document_id,),
        )).fetchone()
        chunks = await (await conn.execute(
            "SELECT count(*) AS n FROM chunks WHERE document_id=%s", (document_id,)
        )).fetchone()
    assert doc["status"] == "cancelled"
    assert doc["chunks_total"] == 0
    assert doc["chunks_embedded"] == 0
    assert chunks["n"] == 0


@pytest.mark.asyncio
async def test_cancel_active_document_requests_cooperative_cancellation(live_db, api_client):
    collection_id = uuid.uuid4()
    document_id = uuid.uuid4()
    async with live_db.pool.connection() as conn:
        await _insert_collection(conn, collection_id, f"cancel-active-{collection_id}")
        await _insert_document(conn, document_id, collection_id, status="embedding")

    response = await api_client.post(f"/api/documents/{document_id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelling"
    assert response.json()["unchanged"] is False
    async with live_db.pool.connection() as conn:
        row = await (await conn.execute(
            "SELECT status FROM documents WHERE id=%s", (document_id,)
        )).fetchone()
    assert row["status"] == "cancelling"

@pytest.mark.asyncio
async def test_ingestor_finalizes_cancelling_document_and_cleans_partial_chunks(live_db, monkeypatch):
    collection_id = uuid.uuid4()
    document_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    monkeypatch.setattr(ingest_module, "db", live_db)
    monkeypatch.setattr(ingest_module, "settings", _FakeIngestSettings())

    async with live_db.pool.connection() as conn:
        await _insert_collection(conn, collection_id, f"cancel-worker-{collection_id}", model="integration-test", dim=3)
        await _insert_document(conn, document_id, collection_id, status="cancelling")
        await conn.execute(
            """INSERT INTO chunks(id, document_id, collection_id, seq, content, embedded)
               VALUES (%s,%s,%s,0,'partial',FALSE)""",
            (chunk_id, document_id, collection_id),
        )
        doc = await (await conn.execute(
            "SELECT * FROM documents WHERE id=%s", (document_id,)
        )).fetchone()

    ingestor = DocumentIngestor(IngestStore(), asyncio.Event())
    await ingestor.process(doc)

    async with live_db.pool.connection() as conn:
        state = await (await conn.execute(
            "SELECT status, chunks_total, chunks_embedded FROM documents WHERE id=%s",
            (document_id,),
        )).fetchone()
        chunk_count = await (await conn.execute(
            "SELECT count(*) AS n FROM chunks WHERE document_id=%s", (document_id,)
        )).fetchone()

    assert state["status"] == "cancelled"
    assert state["chunks_total"] == 0
    assert state["chunks_embedded"] == 0
    assert chunk_count["n"] == 0

@pytest.mark.asyncio
async def test_ingestor_finalizes_cancelling_document_and_cleans_partial_chunks(live_db, monkeypatch):
    collection_id = uuid.uuid4()
    document_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    monkeypatch.setattr(ingest_module, "db", live_db)
    monkeypatch.setattr(ingest_module, "settings", _FakeIngestSettings())

    async with live_db.pool.connection() as conn:
        await _insert_collection(conn, collection_id, f"cancel-worker-{collection_id}", model="integration-test", dim=3)
        await _insert_document(conn, document_id, collection_id, status="cancelling")
        await conn.execute(
            """INSERT INTO chunks(id, document_id, collection_id, seq, content, embedded)
               VALUES (%s,%s,%s,0,'partial',FALSE)""",
            (chunk_id, document_id, collection_id),
        )
        doc = await (await conn.execute(
            "SELECT * FROM documents WHERE id=%s", (document_id,)
        )).fetchone()

    ingestor = DocumentIngestor(IngestStore(), asyncio.Event())
    await ingestor.process(doc)

    async with live_db.pool.connection() as conn:
        state = await (await conn.execute(
            "SELECT status, chunks_total, chunks_embedded FROM documents WHERE id=%s",
            (document_id,),
        )).fetchone()
        chunk_count = await (await conn.execute(
            "SELECT count(*) AS n FROM chunks WHERE document_id=%s", (document_id,)
        )).fetchone()

    assert state["status"] == "cancelled"
    assert state["chunks_total"] == 0
    assert state["chunks_embedded"] == 0
    assert chunk_count["n"] == 0

@pytest.mark.asyncio
async def test_core_collection_default_search_policy(live_db, monkeypatch):
    import app.library_taxonomy as taxonomy

    class FakeSettings:
        async def effective(self):
            return {
                "ingest": {
                    "chunk_size": 1500, "chunk_overlap": 200, "min_chunk_size": 120,
                    "strategy": "recursive", "separators": ["\n\n", "\n", ". ", " "],
                    "embed_batch_size": 16,
                }
            }
        async def get(self, key, default=None):
            return "integration-embedding" if key == "embedding_model" else default

    # Isolate this policy check from legacy shelves by precreating only core names.
    async with live_db.pool.connection() as conn:
        for name in ("Computer Books", "Knowledge Base", "RAG Source Code"):
            await conn.execute(
                """INSERT INTO collections(id,name,ingest_config,default_search)
                   VALUES (%s,%s,'{}'::jsonb,true)
                   ON CONFLICT (name) DO NOTHING""",
                (uuid.uuid4(), name),
            )

    result = await taxonomy.ensure_core_collections(live_db, FakeSettings())
    assert result["collections"]["Computer Books"]["default_search"] is True
    assert result["collections"]["Knowledge Base"]["default_search"] is False
    assert result["collections"]["RAG Source Code"]["default_search"] is False


@pytest.mark.asyncio
async def test_collection_resolution_uses_default_search_unless_explicit(live_db, monkeypatch):
    await live_db.apply_schema()
    enabled_id = uuid.uuid4()
    disabled_id = uuid.uuid4()
    enabled_name = f"enabled-{uuid.uuid4().hex[:8]}"
    disabled_name = f"disabled-{uuid.uuid4().hex[:8]}"
    async with live_db.pool.connection() as conn:
        await conn.execute(
            """INSERT INTO collections(id,name,embedding_model,embedding_dim,default_search)
               VALUES (%s,%s,'integration-test',3,TRUE),
                      (%s,%s,'integration-test',3,FALSE)""",
            (enabled_id, enabled_name, disabled_id, disabled_name),
        )

    monkeypatch.setattr(search_module, 'db', live_db)
    filters = search_module.SearchFilters()

    default_rows = await search_module._resolve_collections(None, filters)
    default_names = {row['name'] for row in default_rows}
    assert enabled_name in default_names
    assert disabled_name not in default_names

    explicit_rows = await search_module._resolve_collections([disabled_name], filters)
    assert [row['name'] for row in explicit_rows] == [disabled_name]


@pytest.mark.asyncio
async def test_explicit_missing_collection_is_an_error(live_db, monkeypatch):
    monkeypatch.setattr(search_module, 'db', live_db)
    with pytest.raises(search_module.SearchError, match='coleções não encontradas'):
        await search_module._resolve_collections(['does-not-exist'], search_module.SearchFilters())

@pytest.mark.asyncio
async def test_directory_source_sync_tracks_new_changed_and_deleted_files(live_db, tmp_path, monkeypatch):
    import app.source_sync as source_sync_module

    class FakeSourceSettings:
        async def effective(self):
            return {
                "allowed_extensions": [".txt"],
                "ingest": {"max_file_mb": 10},
                "workflow": {"auto_start_configured_uploads": False},
                "sources": {
                    "default_exclude_patterns": [".git/**"],
                    "interval_options_seconds": [600],
                    "default_interval_seconds": 600,
                    "poll_interval_seconds": 30,
                    "error_retry_seconds": 5,
                },
            }

        async def get(self, key, default=None):
            return default

    monkeypatch.setattr(source_sync_module, "db", live_db)
    monkeypatch.setattr(source_sync_module, "settings", FakeSourceSettings())
    data_dir = tmp_path / "data"
    monkeypatch.setattr(source_sync_module.env, "data_dir", data_dir)

    root = tmp_path / "source"
    root.mkdir()
    document = root / "notes.txt"
    document.write_text("version one with enough useful text", encoding="utf-8")

    collection_id = uuid.uuid4()
    source_id = uuid.uuid4()
    async with live_db.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO collections(id,name,ingest_config) VALUES (%s,%s,'{}'::jsonb)",
            (collection_id, f"source-sync-{collection_id}"),
        )
        await conn.execute(
            """INSERT INTO collection_sources(
                   id, collection_id, path, interval_seconds, exclude_patterns, next_sync_at)
               VALUES (%s,%s,%s,600,'[]'::jsonb,now())""",
            (source_id, collection_id, str(root)),
        )

    first = await source_sync_module.sync_collection_source(str(source_id))
    assert first["new"] == 1
    async with live_db.pool.connection() as conn:
        row = await (await conn.execute(
            "SELECT id, content_sha256, status, source_path FROM documents WHERE source_id=%s",
            (source_id,),
        )).fetchone()
    assert row["source_path"] == "notes.txt"
    assert row["status"] == "staged"
    document_id = row["id"]
    first_digest = row["content_sha256"]

    document.write_text("version two changed content with enough useful text", encoding="utf-8")
    second = await source_sync_module.sync_collection_source(str(source_id))
    assert second["changed"] == 1
    async with live_db.pool.connection() as conn:
        changed = await (await conn.execute(
            "SELECT id, content_sha256 FROM documents WHERE source_id=%s", (source_id,)
        )).fetchone()
    assert changed["id"] == document_id
    assert changed["content_sha256"] != first_digest

    document.unlink()
    third = await source_sync_module.sync_collection_source(str(source_id))
    assert third["deleted"] == 1
    async with live_db.pool.connection() as conn:
        count = await (await conn.execute(
            "SELECT count(*) AS n FROM documents WHERE source_id=%s", (source_id,)
        )).fetchone()
    assert count["n"] == 0

@pytest.mark.asyncio
async def test_automatic_book_import_targets_computer_books(live_db, monkeypatch):
    import app.api as api_for_auto

    class FakeSettings:
        async def effective(self):
            return {
                "ingest": {
                    "chunk_size": 1500, "chunk_overlap": 200, "min_chunk_size": 120,
                    "strategy": "recursive", "separators": ["\n\n", "\n", ". ", " "],
                    "embed_batch_size": 16,
                }
            }
        async def get(self, key, default=None):
            return "text-embedding-qwen3-embedding-8b" if key == "embedding_model" else default

    async with live_db.pool.connection() as conn:
        await conn.execute(
            """INSERT INTO collections(
                   id,name,description,ingest_config,default_search,embedding_model)
               VALUES (%s,'Computer Books','books','{}'::jsonb,true,%s)
               ON CONFLICT (name) DO NOTHING""",
            (uuid.uuid4(), "text-embedding-qwen3-embedding-8b"),
        )

    monkeypatch.setattr(api_for_auto, "db", live_db)
    monkeypatch.setattr(api_for_auto, "settings", FakeSettings())
    inbox = await api_for_auto._ensure_auto_inbox()
    assert inbox["name"] == "Computer Books"
    assert inbox["default_search"] is True


@pytest.mark.asyncio
async def test_auto_category_applies_only_above_configured_confidence(live_db, monkeypatch):
    import app.classification_runtime as classification_module

    class FakeSettings:
        async def effective(self):
            return {
                "classification": {
                    "confidence_threshold": 0.92,
                    "auto_apply_existing": True,
                },
            }

    monkeypatch.setattr(classification_module, "db", live_db)
    monkeypatch.setattr(classification_module, "settings", FakeSettings())

    books_id = uuid.uuid4()
    category_id = uuid.uuid4()
    high_id, low_id = uuid.uuid4(), uuid.uuid4()
    model = "text-embedding-qwen3-embedding-8b"
    async with live_db.pool.connection() as conn:
        await conn.execute(
            """INSERT INTO collections(
                   id,name,ingest_config,default_search,embedding_model,embedding_dim)
               VALUES (%s,'Computer Books','{}'::jsonb,true,%s,4096)
               ON CONFLICT (name) DO UPDATE SET embedding_model=EXCLUDED.embedding_model,
                 embedding_dim=EXCLUDED.embedding_dim
               RETURNING id""",
            (books_id, model),
        )
        books = await (
            await conn.execute("SELECT id FROM collections WHERE name='Computer Books'")
        ).fetchone()
        books_id = books["id"]
        await conn.execute(
            """INSERT INTO categories(id,collection_id,name,description)
               VALUES (%s,%s,'Python','Python books')
               ON CONFLICT (collection_id,name) DO NOTHING""",
            (category_id, books_id),
        )
        category = await (
            await conn.execute(
                "SELECT id FROM categories WHERE collection_id=%s AND name='Python'", (books_id,)
            )
        ).fetchone()
        category_id = category["id"]
        for doc_id, digest in ((high_id, "a" * 64), (low_id, "b" * 64)):
            await conn.execute(
                """INSERT INTO documents(
                       id,collection_id,filename,source_path,stored_path,content_sha256,
                       size_bytes,status,meta)
                   VALUES (%s,%s,%s,%s,%s,%s,10,'ready','{"auto_classify":true}'::jsonb)""",
                (doc_id, books_id, f"{doc_id}.txt", f"{doc_id}.txt", f"blobs/{doc_id}", digest),
            )

    high = await classification_module.apply_ready_classification(str(high_id), {
        "existing_category": "Python",
        "confidence": 0.95,
        "proposed_category": None,
        "rationale": "clear Python content",
    })
    low = await classification_module.apply_ready_classification(str(low_id), {
        "existing_category": "Python",
        "confidence": 0.70,
        "proposed_category": None,
        "rationale": "uncertain",
    })

    assert high["auto_applied"] is True
    assert high["routed_to"] == "Python"
    assert low["auto_applied"] is False
    assert low["auto_apply_eligible"] is False

    async with live_db.pool.connection() as conn:
        high_row = await (
            await conn.execute(
                "SELECT collection_id,category_id,status,auto_suggestion FROM documents WHERE id=%s",
                (high_id,),
            )
        ).fetchone()
        low_row = await (
            await conn.execute(
                "SELECT collection_id,category_id,status FROM documents WHERE id=%s", (low_id,)
            )
        ).fetchone()

    assert high_row["collection_id"] == books_id
    assert high_row["category_id"] == category_id
    assert high_row["status"] == "ready"
    assert high_row["auto_suggestion"]["auto_applied"] is True
    assert low_row["collection_id"] == books_id
    assert low_row["category_id"] is None


@pytest.mark.asyncio
async def test_classification_runtime_uses_embedding_model_and_categories(live_db, monkeypatch):
    import app.classification_runtime as classification_module

    books_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    async with live_db.pool.connection() as conn:
        await conn.execute(
            """INSERT INTO collections(
                   id,name,description,ingest_config,embedding_model,embedding_dim)
               VALUES (%s,'Computer Books','test','{}'::jsonb,%s,4096)
               ON CONFLICT (name) DO UPDATE SET embedding_model=EXCLUDED.embedding_model,
                 embedding_dim=EXCLUDED.embedding_dim""",
            (books_id, "text-embedding-qwen3-embedding-8b"),
        )
        books = await (
            await conn.execute("SELECT id FROM collections WHERE name='Computer Books'")
        ).fetchone()
        books_id = books["id"]
        await conn.execute(
            """INSERT INTO categories(id,collection_id,name,description)
               VALUES (%s,%s,'Software Testing','Testing books')
               ON CONFLICT (collection_id,name) DO NOTHING""",
            (uuid.uuid4(), books_id),
        )
        await conn.execute(
            """INSERT INTO documents(
                   id,collection_id,filename,source_path,stored_path,content_sha256,size_bytes,status)
               VALUES (%s,%s,'book.txt','book.txt','blobs/book',%s,10,'ready')""",
            (doc_id, books_id, "c" * 64),
        )

    captured = {}

    class FakeClient:
        pass

    class FakeSettings:
        async def effective(self):
            return {
                "auto": {"sample_chars": 3500, "request_timeout": 240},
                "classification": {
                    "confidence_threshold": 0.55,
                    "auto_apply_existing": True,
                    "embedding_match_threshold": 0.55,
                    "embedding_margin_threshold": 0.03,
                },
            }
        async def get(self, key, default=None):
            return "text-embedding-qwen3-embedding-8b" if key == "embedding_model" else default
        async def lmstudio_client(self, **kwargs):
            return FakeClient()

    class FakeExtracted:
        def full_text(self):
            return "sample text"

    async def fake_sample_extract(doc):
        return FakeExtracted()

    async def fake_classify(client, model, **kwargs):
        captured["model"] = model
        captured["kwargs"] = kwargs
        return {
            "existing_collection": None,
            "confidence": 0.5,
            "proposed_collection": {"name": "New Topic", "description": "new"},
            "new_collection_suggested": True,
            "rationale": "test",
            "source": "embedding",
        }

    monkeypatch.setattr(classification_module, "db", live_db)
    monkeypatch.setattr(classification_module, "settings", FakeSettings())
    monkeypatch.setattr(classification_module, "_sample_extract", fake_sample_extract)
    monkeypatch.setattr(classification_module, "classify_document", fake_classify)

    result = await classification_module.classify_document_runtime(str(doc_id))
    assert result["filename"] == "book.txt"
    assert result["proposed_category"] is None
    assert result["new_collection_suggested"] is False
    assert captured["model"] == "text-embedding-qwen3-embedding-8b"
    assert captured["kwargs"]["match_threshold"] == 0.55
    assert captured["kwargs"]["margin_threshold"] == 0.03
    assert "Software Testing" in [c["name"] for c in captured["kwargs"]["collections"]]


@pytest.mark.asyncio
async def test_bulk_classification_queues_only_ready_documents(live_db, monkeypatch):
    import app.api as api_module

    class FakeSettings:
        async def get(self, key, default=None):
            return "text-embedding-qwen3-embedding-8b" if key == "embedding_model" else default

    monkeypatch.setattr(api_module, "db", live_db)
    monkeypatch.setattr(api_module, "settings", FakeSettings())

    collection_id = uuid.uuid4()
    ready_id = uuid.uuid4()
    pending_id = uuid.uuid4()
    async with live_db.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO collections(id,name,ingest_config) VALUES (%s,%s,'{}'::jsonb)",
            (collection_id, f"Bulk-{collection_id}"),
        )
        await conn.execute(
            """INSERT INTO documents(id,collection_id,filename,source_path,stored_path,content_sha256,size_bytes,status,auto_suggestion,meta)
               VALUES (%s,%s,'ready.txt','ready.txt','blobs/ready',%s,10,'ready','{"confidence":0.5}'::jsonb,'{"auto_classify_attempts":2}'::jsonb)""",
            (ready_id, collection_id, "d" * 64),
        )
        await conn.execute(
            """INSERT INTO documents(id,collection_id,filename,source_path,stored_path,content_sha256,size_bytes,status,meta)
               VALUES (%s,%s,'pending.txt','pending.txt','blobs/pending',%s,10,'pending','{}'::jsonb)""",
            (pending_id, collection_id, "e" * 64),
        )

    result = await api_module.classify_documents_bulk(
        api_module.BulkClassifyRequest(all_filtered=True, collection_id=str(collection_id))
    )
    assert result["queued"] == 1
    assert result["document_ids"] == [str(ready_id)]

    async with live_db.pool.connection() as conn:
        ready = await (await conn.execute(
            "SELECT auto_suggestion,meta FROM documents WHERE id=%s", (ready_id,)
        )).fetchone()
        pending = await (await conn.execute(
            "SELECT meta FROM documents WHERE id=%s", (pending_id,)
        )).fetchone()
    assert ready["auto_suggestion"] is None
    assert ready["meta"]["auto_classify"] is True
    assert "auto_classify_attempts" not in ready["meta"]
    assert pending["meta"].get("auto_classify") is not True


@pytest.mark.asyncio
async def test_lexical_retrieval_uses_real_postgres_and_respects_scope(live_db, monkeypatch):
    monkeypatch.setattr(search_module, "db", live_db)
    col, doc, chunk = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with live_db.pool.connection() as conn:
        await _insert_collection(conn, col, f"lexical-{col}", model="integration-test", dim=3)
        await conn.execute(
            """INSERT INTO documents(id,collection_id,filename,source_path,stored_path,
               content_sha256,size_bytes,status)
               VALUES (%s,%s,'lexical.txt','lexical.txt','blobs/test',%s,100,'ready')""",
            (doc,col,uuid.uuid4().hex))
        await conn.execute(
            """INSERT INTO chunks(id,document_id,collection_id,seq,content,embedded)
               VALUES (%s,%s,%s,0,'Quizzical platypuses retrieve documents efficiently.',TRUE)""",
            (chunk,doc,col))
    rows = await search_module._lexical_search("How does a quizzical platypus retrieve?", [{"id":col}], search_module.SearchFilters(), 5)
    assert rows and rows[0]["chunk_id"] == chunk
    assert rows[0]["lexical_score"] > 0
    assert not await search_module._lexical_search("quizzical", [{"id":uuid.uuid4()}], search_module.SearchFilters(), 5)

@pytest.mark.asyncio
async def test_high_dim_index_migrates_existing_vectors_and_refines(live_db, monkeypatch):
    from app.search_filters import SearchFilters
    dim = 4096
    cid, did, near, far = [uuid.uuid4() for _ in range(4)]
    q = [1.0, -1.0] * (dim // 2)
    async with live_db.pool.connection() as conn:
        await conn.execute("INSERT INTO collections(id,name,embedding_model,embedding_dim) VALUES (%s,%s,'fixture',%s)", (cid,str(cid),dim))
        await conn.execute("""INSERT INTO documents(id,collection_id,filename,source_path,stored_path,content_sha256,status)
            VALUES (%s,%s,'fixture','fixture','fixture',%s,'ready')""",(did,cid,str(did)))
        await conn.execute("""INSERT INTO chunks(id,document_id,collection_id,seq,content,embedded)
            VALUES (%s,%s,%s,0,'near',true),(%s,%s,%s,1,'far',true)""",(near,did,cid,far,did,cid))
        await conn.execute(f"CREATE TABLE IF NOT EXISTS vec_{dim}(chunk_id uuid PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,embedding vector({dim}) NOT NULL)")
        await conn.execute(f"INSERT INTO vec_{dim} VALUES (%s,%s::vector),(%s,%s::vector)",(near,vector_literal(q),far,vector_literal([-v for v in q])))
    await live_db.apply_schema()
    await live_db.apply_schema()
    async with live_db.pool.connection() as conn:
        row = await (await conn.execute("SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass('vec_4096_binary_hnsw')")).fetchone()
        assert row["indisvalid"]
        row = await (await conn.execute("SELECT vector_dims(embedding) AS dim FROM vec_4096 WHERE chunk_id=%s",(near,))).fetchone()
        assert row["dim"] == dim
    class FakeClient:
        async def embed(self, texts, model):
            return [q]
    monkeypatch.setattr(search_module,"db",live_db)
    args=dict(query="fixture", model="fixture", dim=dim,group_cols=[{"id":cid}],
              filters=SearchFilters(),top_k=1,client=FakeClient())
    cfg={"hnsw_ef_search":64,"binary_hnsw_enabled":True,"binary_candidate_multiplier":8}
    indexed,_,_=await search_module._search_group(**args,scfg=cfg)
    exact,_,_=await search_module._search_group(**args,scfg={**cfg,"binary_hnsw_enabled":False})
    assert indexed[0]["chunk_id"] == exact[0]["chunk_id"] == near
    assert indexed[0]["score"] == pytest.approx(exact[0]["score"])


@pytest.mark.asyncio
async def test_injected_failure_rolls_back_transaction(live_db):
    collection_id = uuid.uuid4()
    try:
        async with live_db.pool.connection() as conn:
            async with conn.transaction():
                await _insert_collection(conn, collection_id, f"rollback-{collection_id}")
                raise RuntimeError("injected failure after write")
    except RuntimeError:
        pass

    async with live_db.pool.connection() as conn:
        row = await (
            await conn.execute("SELECT count(*) AS n FROM collections WHERE id=%s", (collection_id,))
        ).fetchone()
    assert row["n"] == 0


@pytest.mark.asyncio
async def test_postgres_deadlock_aborts_one_transaction_and_recovers(live_db):
    first = uuid.uuid4()
    second = uuid.uuid4()
    async with live_db.pool.connection() as conn:
        await _insert_collection(conn, first, f"deadlock-a-{first}")
        await _insert_collection(conn, second, f"deadlock-b-{second}")

    first_locked = asyncio.Event()
    second_locked = asyncio.Event()

    async def worker(conn, own_id, other_id, mine, other):
        try:
            async with conn.transaction():
                await conn.execute("SET LOCAL deadlock_timeout = '100ms'")
                await conn.execute("UPDATE collections SET description='locked' WHERE id=%s", (own_id,))
                mine.set()
                await asyncio.wait_for(other.wait(), timeout=2)
                await conn.execute("UPDATE collections SET description='completed' WHERE id=%s", (other_id,))
            return "ok"
        except Exception as exc:
            return exc

    async with live_db.pool.connection() as conn1, live_db.pool.connection() as conn2:
        outcomes = await asyncio.wait_for(
            asyncio.gather(
                worker(conn1, first, second, first_locked, second_locked),
                worker(conn2, second, first, second_locked, first_locked),
            ),
            timeout=5,
        )

    deadlocks = [x for x in outcomes if isinstance(x, psycopg.errors.DeadlockDetected)]
    assert len(deadlocks) == 1
    assert "ok" in outcomes

    async with live_db.pool.connection() as conn:
        row = await (await conn.execute("SELECT 1 AS ok")).fetchone()
    assert row["ok"] == 1


@pytest.mark.asyncio
async def test_concurrent_setting_writes_do_not_cross_talk(live_db):
    prefix = uuid.uuid4().hex
    pairs = [(f"concurrency.{prefix}.{i}", {"worker": i}) for i in range(24)]
    await asyncio.gather(*(live_db.set_setting(key, value) for key, value in pairs))
    values = await asyncio.gather(*(live_db.get_setting(key) for key, _ in pairs))
    assert values == [value for _, value in pairs]


@pytest.mark.asyncio
async def test_repository_corpus_bootstrap_and_sync_preserve_git_provenance(
    live_db, tmp_path, monkeypatch
):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "sample.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "sample.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()

    effective = {
        "repository_corpus": {
            "enabled": True,
            "path": str(repo),
            "collection_name": "RAG Source Code Integration",
            "description": "integration repo",
            "default_search": False,
            "interval_seconds": 600,
            "exclude_patterns": [".git/**"],
        },
        "sources": {
            "default_interval_seconds": 600,
            "interval_options_seconds": [600],
            "default_exclude_patterns": [".git/**", "__pycache__/**"],
        },
        "allowed_extensions": [".py"],
        "ingest": {"max_file_mb": 10},
        "workflow": {"auto_start_configured_uploads": False},
    }

    async def fake_effective():
        return effective

    async def fake_get(key, default=None):
        return "integration-embedding" if key == "embedding_model" else default

    async def fake_resolve(collection_cfg, override):
        return {
            "chunk_size": 1500,
            "chunk_overlap": 200,
            "min_chunk_size": 120,
            "strategy": "recursive",
            "separators": ["\n\n", "\n", ". ", " "],
            "embedding_model": "integration-embedding",
            "embed_batch_size": 16,
        }

    monkeypatch.setattr(source_sync_module, "db", live_db)
    monkeypatch.setattr(source_sync_module.settings, "effective", fake_effective)
    monkeypatch.setattr(source_sync_module.settings, "get", fake_get)
    monkeypatch.setattr(source_sync_module, "resolve_ingest_config", fake_resolve)
    monkeypatch.setattr(source_sync_module.env, "data_dir", tmp_path / "data")

    boot = await source_sync_module.ensure_repository_corpus()
    assert boot is not None
    assert boot["collection"]["default_search"] is False
    assert boot["source"]["source_type"] == "repository"

    stats = await source_sync_module.sync_collection_source(str(boot["source"]["id"]))
    assert stats["new"] == 1

    async with live_db.pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT source_path, status, meta FROM documents WHERE source_id=%s",
                (boot["source"]["id"],),
            )
        ).fetchone()
    assert row["source_path"] == "sample.py"
    assert row["status"] == "staged"
    assert row["meta"]["source_type"] == "repository"
    assert row["meta"]["repository_commit"] == commit
    assert row["meta"]["repository_path"] == "sample.py"


@pytest.mark.asyncio
async def test_core_collection_migration_consolidates_legacy_shelves_without_inferred_categories(
    live_db, monkeypatch
):
    import app.library_taxonomy as taxonomy

    legacy_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    digest = uuid.uuid4().hex

    async with live_db.pool.connection() as conn:
        await conn.execute("DELETE FROM settings WHERE key='library_taxonomy_version'")
        await conn.execute(
            """INSERT INTO collections(id,name,description,ingest_config,default_search)
               VALUES (%s,'Legacy Python','Old shelf','{}'::jsonb,true)""",
            (legacy_id,),
        )
        await conn.execute(
            """INSERT INTO documents(
                   id,collection_id,filename,source_path,stored_path,content_sha256,size_bytes,status)
               VALUES (%s,%s,'python.epub','python.epub','blobs/python',%s,1,'ready')""",
            (doc_id, legacy_id, digest),
        )
        await conn.execute(
            """INSERT INTO chunks(id,document_id,collection_id,seq,content,embedded)
               VALUES (%s,%s,%s,0,'python content',true)""",
            (chunk_id, doc_id, legacy_id),
        )

    class FakeSettings:
        async def effective(self):
            return {
                "ingest": {
                    "chunk_size": 1500, "chunk_overlap": 200, "min_chunk_size": 120,
                    "strategy": "recursive", "separators": ["\n\n", "\n", ". ", " "],
                    "embed_batch_size": 16,
                }
            }
        async def get(self, key, default=None):
            return "integration-embedding" if key == "embedding_model" else default

    result = await taxonomy.ensure_core_collections(
        live_db, FakeSettings(), apply_migration=True
    )
    books_id = result["collections"][taxonomy.BOOK_LIBRARY]["id"]

    async with live_db.pool.connection() as conn:
        inferred = await (
            await conn.execute(
                "SELECT id FROM categories WHERE collection_id=%s AND name='Legacy Python'",
                (books_id,),
            )
        ).fetchone()
        categories = await (
            await conn.execute(
                "SELECT name FROM categories WHERE collection_id=%s ORDER BY name",
                (books_id,),
            )
        ).fetchall()
        doc = await (
            await conn.execute(
                "SELECT collection_id,category_id FROM documents WHERE id=%s", (doc_id,)
            )
        ).fetchone()
        chunk = await (
            await conn.execute(
                "SELECT collection_id FROM chunks WHERE id=%s", (chunk_id,)
            )
        ).fetchone()

    assert inferred is None
    assert categories == []
    assert doc["collection_id"] == books_id
    assert doc["category_id"] is None
    assert chunk["collection_id"] == books_id

    async with live_db.pool.connection() as conn:
        await conn.execute(
            """INSERT INTO categories(id,collection_id,name,description)
               VALUES (%s,%s,'Economics','Economics books.')""",
            (uuid.uuid4(), books_id),
        )
    again = await taxonomy.ensure_core_collections(
        live_db, FakeSettings(), apply_migration=True
    )
    assert again["taxonomy_reset"] is False
    async with live_db.pool.connection() as conn:
        custom = await (
            await conn.execute(
                "SELECT id FROM categories WHERE collection_id=%s AND name='Economics'",
                (books_id,),
            )
        ).fetchone()
    assert custom is not None


@pytest.mark.asyncio
async def test_bad_v1_computer_books_name_is_repaired_not_kept_as_category(live_db):
    import app.library_taxonomy as taxonomy

    async with live_db.pool.connection() as conn:
        await conn.execute("DELETE FROM settings WHERE key='library_taxonomy_version'")
        canonical = await (
            await conn.execute("SELECT id FROM collections WHERE name='Computer Books'")
        ).fetchone()
        if canonical:
            await conn.execute("DELETE FROM documents WHERE collection_id=%s", (canonical["id"],))
            await conn.execute("DELETE FROM collections WHERE id=%s", (canonical["id"],))
        await conn.execute(
            """INSERT INTO collections(id,name,description,ingest_config,default_search)
               VALUES (%s,'Computer Books','bad v1 name','{}'::jsonb,true)
               ON CONFLICT (name) DO NOTHING""",
            (uuid.uuid4(),),
        )

    class FakeSettings:
        async def effective(self):
            return {"ingest": {"chunk_size":1500,"chunk_overlap":200,"min_chunk_size":120,
                "strategy":"recursive","separators":["\n\n","\n",". "," "],"embed_batch_size":16}}
        async def get(self, key, default=None):
            return "integration-embedding" if key == "embedding_model" else default

    await taxonomy.ensure_core_collections(live_db, FakeSettings(), apply_migration=True)
    async with live_db.pool.connection() as conn:
        names = [r["name"] for r in await (
            await conn.execute("SELECT name FROM collections ORDER BY name")
        ).fetchall()]
        bad_category = await (
            await conn.execute("SELECT id FROM categories WHERE name='Computer Books'")
        ).fetchone()
    assert "Computer Books" in names
    assert "Informática" not in names
    assert bad_category is None


@pytest.mark.asyncio
async def test_classifier_cannot_create_categories_automatically(live_db, monkeypatch):
    import app.api as api_for_categories
    import app.library_taxonomy as taxonomy

    class FakeSettings:
        async def effective(self):
            return {
                "ingest": {
                    "chunk_size": 1500, "chunk_overlap": 200, "min_chunk_size": 120,
                    "strategy": "recursive", "separators": ["\n\n", "\n", ". ", " "],
                    "embed_batch_size": 16,
                }
            }
        async def get(self, key, default=None):
            return "integration-embedding" if key == "embedding_model" else default

    core = await taxonomy.ensure_core_collections(live_db, FakeSettings())
    books = core["collections"][taxonomy.BOOK_LIBRARY]
    doc_id = uuid.uuid4()
    proposal = {
        "proposed_category": {"name": "Economics", "description": "Economics books."},
        "proposed_collection": {"name": "Economics", "description": "Economics books."},
        "confidence": 0.42,
        "auto_applied": False,
    }
    async with live_db.pool.connection() as conn:
        await conn.execute(
            """INSERT INTO documents(
                   id,collection_id,filename,source_path,stored_path,content_sha256,
                   size_bytes,status,auto_suggestion,meta)
               VALUES (%s,%s,'econ.epub','econ.epub','blobs/econ',%s,1,'ready',%s::jsonb,'{}'::jsonb)""",
            (doc_id, books["id"], uuid.uuid4().hex, json.dumps(proposal)),
        )

    monkeypatch.setattr(api_for_categories, "db", live_db)
    app = FastAPI()
    app.include_router(api_for_categories.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/documents/{doc_id}/classification-decision", json={"accept": True}
        )
    assert response.status_code == 409

    async with live_db.pool.connection() as conn:
        created = await (
            await conn.execute("SELECT id FROM categories WHERE name='Economics'")
        ).fetchone()
        doc = await (
            await conn.execute("SELECT category_id FROM documents WHERE id=%s", (doc_id,))
        ).fetchone()
    assert created is None
    assert doc["category_id"] is None


@pytest.mark.asyncio
async def test_post_migration_default_search_still_resolves_computer_books(live_db, monkeypatch):
    import app.library_taxonomy as taxonomy
    import app.search as search_module

    class FakeSettings:
        async def effective(self):
            return {
                "ingest": {
                    "chunk_size": 1500, "chunk_overlap": 200, "min_chunk_size": 120,
                    "strategy": "recursive", "separators": ["\n\n", "\n", ". ", " "],
                    "embed_batch_size": 16,
                }
            }
        async def get(self, key, default=None):
            return "text-embedding-qwen3-embedding-8b" if key == "embedding_model" else default

    async with live_db.pool.connection() as conn:
        await conn.execute("DELETE FROM settings WHERE key='library_taxonomy_version'")
        await conn.execute(
            """INSERT INTO collections(id,name,ingest_config,default_search,embedding_model,embedding_dim)
               VALUES (%s,'Knowledge Base','{}'::jsonb,false,%s,4096)
               ON CONFLICT (name) DO UPDATE SET embedding_model=EXCLUDED.embedding_model,
                 embedding_dim=4096,default_search=false""",
            (uuid.uuid4(), "text-embedding-qwen3-embedding-8b"),
        )
        await conn.execute(
            """INSERT INTO collections(id,name,ingest_config,default_search,embedding_model,embedding_dim)
               VALUES (%s,'Informática','{}'::jsonb,true,%s,NULL)
               ON CONFLICT (name) DO UPDATE SET embedding_model=EXCLUDED.embedding_model,
                 embedding_dim=NULL,default_search=true""",
            (uuid.uuid4(), "text-embedding-qwen3-embedding-8b"),
        )

    result = await taxonomy.ensure_core_collections(
        live_db, FakeSettings(), apply_migration=True
    )
    books = result["collections"]["Computer Books"]
    assert books["embedding_dim"] == 4096

    monkeypatch.setattr(search_module, "db", live_db)
    cols = await search_module._resolve_collections(None, search_module.SearchFilters())
    assert "Computer Books" in [c["name"] for c in cols]


@pytest.mark.asyncio
async def test_deleting_category_leaves_books_uncategorized(live_db):
    books_id = uuid.uuid4()
    category_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    async with live_db.pool.connection() as conn:
        await conn.execute(
            """INSERT INTO collections(id,name,ingest_config,default_search)
               VALUES (%s,'Computer Books','{}'::jsonb,true)
               ON CONFLICT (name) DO NOTHING""",
            (books_id,),
        )
        books = await (
            await conn.execute("SELECT id FROM collections WHERE name='Computer Books'")
        ).fetchone()
        books_id = books["id"]
        await conn.execute(
            """INSERT INTO categories(id,collection_id,name,description)
               VALUES (%s,%s,'Economics','Economics books')
               ON CONFLICT (collection_id,name) DO UPDATE SET name=EXCLUDED.name
               RETURNING id""",
            (category_id, books_id),
        )
        category = await (
            await conn.execute(
                "SELECT id FROM categories WHERE collection_id=%s AND name='Economics'",
                (books_id,),
            )
        ).fetchone()
        category_id = category["id"]
        await conn.execute(
            """INSERT INTO documents(
                   id,collection_id,category_id,filename,source_path,stored_path,
                   content_sha256,size_bytes,status)
               VALUES (%s,%s,%s,'econ.epub','econ.epub','blobs/econ',%s,1,'ready')""",
            (doc_id, books_id, category_id, uuid.uuid4().hex),
        )
        await conn.execute("DELETE FROM categories WHERE id=%s", (category_id,))
        doc = await (
            await conn.execute(
                "SELECT collection_id,category_id FROM documents WHERE id=%s", (doc_id,)
            )
        ).fetchone()

    assert doc["collection_id"] == books_id
    assert doc["category_id"] is None
