from pathlib import Path


def test_binary_hnsw_candidate_stage_keeps_document_filters_after_shortlist():
    search = Path("app/search.py").read_text()
    start = search.index("WITH candidates AS MATERIALIZED (")
    end = search.index("SELECT c.id AS chunk_id", start)
    candidate_stage = search[start:end]

    assert "JOIN documents" not in candidate_stage
    assert "superseded_by" not in search
    assert "WHERE d.status='ready'" in search[end:]


def test_search_requires_exact_embedding_reranker_model_profile():
    search = Path("app/search.py").read_text()
    assert "runtime_scheduler.ensure_resident(client, requirements, exact=True)" in search
