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
