import pytest

from app.collection_classifier import classify_document, collection_profile, cosine_similarity, representative_sample

COLLECTIONS = [
    {"name": "Python", "description": "Python books", "doc_count": 4, "examples": ["Fluent Python"]},
    {"name": "RAG & AI Agents", "description": "RAG and agents", "doc_count": 7, "examples": ["RAG Cookbook"]},
]


class FakeClient:
    def __init__(self, vectors):
        self.vectors = vectors

    async def embed(self, texts, model):
        return self.vectors


def test_collection_profile_uses_taxonomy_evidence():
    p = collection_profile(COLLECTIONS[0])
    assert "Category: Python" in p and "Python books" in p and "Fluent Python" in p


def test_cosine_similarity():
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_embedding_classifier_selects_strong_existing_category():
    client = FakeClient([[1, 0], [0.9, 0.1], [0.2, 0.8]])
    result = await classify_document(
        client, "embed", filename="x", source_path="x", text="content",
        collections=COLLECTIONS, sample_chars=900, timeout=5,
        match_threshold=0.55, margin_threshold=0.03,
    )
    assert result["existing_collection"] == "Python"
    assert result["source"] == "embedding"
    assert result["new_collection_suggested"] is False


@pytest.mark.asyncio
async def test_embedding_classifier_defers_ambiguous_match_without_creating_category():
    client = FakeClient([[1, 0], [0.8, 0.6], [0.79, 0.61]])
    result = await classify_document(
        client, "embed", filename="x", source_path="x", text="content",
        collections=COLLECTIONS, sample_chars=900, timeout=5,
        match_threshold=0.55, margin_threshold=0.03,
    )
    assert result["existing_collection"] is None
    assert result["proposed_collection"] is None
    assert result["new_collection_suggested"] is True


def test_representative_sample_spreads_across_document():
    text = "BEGIN_MARKER " + ("a" * 3000) + " MIDDLE_MARKER " + ("b" * 3000) + " END_MARKER"
    sample = representative_sample(text, 900)
    assert "BEGIN_MARKER" in sample
    assert "MIDDLE_MARKER" in sample
    assert "END_MARKER" in sample
