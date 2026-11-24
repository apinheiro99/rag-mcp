from app.graph_retrieval import evidence_graph


def test_cross_source_shared_mentions_are_not_semantic_claims():
    rows = [
        {"chunk_id":"a","document_id":"1","document":"First","location":{},"content":"Graph search uses RAG."},
        {"chunk_id":"b","document_id":"2","document":"Second","location":{},"content":"RAG can use graphs."},
        {"chunk_id":"c","document_id":"1","document":"First","location":{},"content":"RAG is useful."},
    ]
    g=evidence_graph(rows,"RAG",10)
    assert len(g["nodes"])==3
    assert len(g["edges"])==2
    assert all(e["type"]=="shared_mention" and e["query_match"] for e in g["edges"])
    assert len(evidence_graph(rows,"RAG",1)["edges"])==1
