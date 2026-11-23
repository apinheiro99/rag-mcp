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
from app.db import Database, vector_literal
from app.ingest import DocumentIngestor, IngestStore


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
async def test_create_collection_inherits_effective_ingest_defaults(api_client, monkeypatch):
    async def fake_effective():
        return {
            "ingest": {
                "chunk_size": 1500,
                "chunk_overlap": 200,
                "min_chunk_size": 120,
                "strategy": "recursive",
                "separators": ["\n\n", "\n", ". ", " "],
                "embed_batch_size": 16,
            },
            "limits": {
                "chunk_size_range": [200, 8000],
                "chunk_overlap_range": [0, 1000],
                "min_chunk_size_range": [0, 2000],
                "embed_batch_size_range": [1, 128],
            },
        }

    monkeypatch.setattr(api_module.settings, "effective", fake_effective)
    name = f"defaults-{uuid.uuid4().hex}"
    response = await api_client.post("/api/collections", json={"name": name})

    assert response.status_code == 201
    body = response.json()
    assert body["ingest_config"]["strategy"] == "recursive"
    assert body["ingest_config"]["chunk_size"] == 1500
    assert body["ingest_config"]["chunk_overlap"] == 200
    assert body["ingest_config"]["embed_batch_size"] == 16


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
    async def embed(self, texts, model):
        return [[1.0, 0.0, 0.0] for _ in texts]


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
