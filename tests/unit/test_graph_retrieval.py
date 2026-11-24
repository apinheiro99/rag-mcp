import pytest
from app.graph_retrieval import evidence_graph, extract_concepts


def test_extract_concepts_normalizes_named_mentions():
    concepts = extract_concepts("Microsoft GraphRAG works with RAG and Knowledge Graphs.")
    assert "microsoft graphrag" in concepts
    assert "rag" in concepts
    assert "knowledge graphs" in concepts


def test_evidence_graph_links_cross_document_shared_mentions():
    rows = [
        {"chunk_id": "a", "document_id": "d1", "content": "Microsoft GraphRAG uses RAG.", "document": "one", "location": {}},
        {"chunk_id": "b", "document_id": "d2", "content": "RAG and Microsoft GraphRAG.", "document": "two", "location": {}},
    ]
    graph = evidence_graph(rows, "RAG", 10)
    assert graph["persistent"] is False
    assert graph["edges"]
    assert any(edge["concept"] == "rag" for edge in graph["edges"])
    assert len(evidence_graph(rows, "RAG", 1)["edges"]) == 1

@pytest.mark.asyncio
async def test_persistent_graph_skips_when_materialization_is_missing(monkeypatch):
    import app.graph_retrieval as gr

    class FakeCursor:
        async def fetchall(self):
            return []

    class FakeConn:
        async def execute(self, *args, **kwargs):
            return FakeCursor()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False

    class FakePool:
        def connection(self):
            return FakeConn()

    class FakeDB:
        pool = FakePool()

    monkeypatch.setattr(gr, "db", FakeDB())
    rows = [{
        "chunk_id": "11111111-1111-1111-1111-111111111111",
        "document_id": "22222222-2222-2222-2222-222222222222",
        "content": "RAG",
        "document": "doc",
        "location": {},
    }]
    graph = await gr.persistent_evidence_graph(rows, "RAG", 10)
    assert graph["available"] is False
    assert graph["reason"] == "graph_not_materialized_yet"
    assert graph["edges"] == []

@pytest.mark.asyncio
async def test_graph_worker_survives_iteration_failure(monkeypatch):
    import asyncio
    import app.graph_retrieval as gr

    worker = gr.GraphWorker(poll_interval=0.001)
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        worker._stop.set()
        return None

    monkeypatch.setattr(worker, "_next_stale_document", boom)
    worker._stop = asyncio.Event()
    await asyncio.wait_for(worker._run(), timeout=0.1)
    assert calls["n"] >= 2
