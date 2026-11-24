import pytest

from app.collection_classifier import parse_category_proposal, classify_document, collection_profile, cosine_similarity, representative_sample, suggest_category

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
    assert result["proposed_collection"] == {
        "name": "x",
        "description": "Documents primarily about x.",
    }
    assert result["new_collection_suggested"] is True


def test_representative_sample_spreads_across_document():
    text = "BEGIN_MARKER " + ("a" * 3000) + " MIDDLE_MARKER " + ("b" * 3000) + " END_MARKER"
    sample = representative_sample(text, 900)
    assert "BEGIN_MARKER" in sample
    assert "MIDDLE_MARKER" in sample
    assert "END_MARKER" in sample


def test_suggest_category_from_book_title():
    suggestion = suggest_category(
        "The Art of Software Testing, 3rd Edition.pdf",
        "The Art of Software Testing, 3rd Edition.pdf",
    )
    assert suggestion["name"] == "Software Testing"
    assert "Software Testing" in suggestion["description"]


def test_suggest_category_strips_author_suffix():
    suggestion = suggest_category(
        "Automating DevOps with GitLab CI-CD Pipelines - Build -- Jane Doe -- ( WeLib.gl ).epub",
        "",
    )
    assert suggestion["name"] == "DevOps GitLab"


def test_parse_category_proposal_accepts_short_semantic_category():
    proposal = parse_category_proposal('{"name":"Economics","description":"Economics books."}')
    assert proposal == {"name": "Economics", "description": "Economics books."}


def test_parse_category_proposal_rejects_title_like_long_name():
    import pytest
    with pytest.raises(ValueError, match="1-3 word"):
        parse_category_proposal('{"name":"The Complete Economics Handbook Fifth Edition","description":"x"}')


class FakeHybridClient:
    def __init__(self, vectors, chat_response):
        self.vectors = vectors
        self.chat_response = chat_response
        self.chat_calls = 0

    async def embed(self, texts, model):
        return self.vectors

    async def chat(self, messages, model, **kwargs):
        self.chat_calls += 1
        return self.chat_response


@pytest.mark.asyncio
async def test_llm_existing_category_choice_is_restricted_to_catalog():
    from app.collection_classifier import classify_existing_category_with_llm

    client = FakeHybridClient(
        [[1, 0]],
        '{"category":"Economics","confidence":0.99,"rationale":"invented"}',
    )
    with pytest.raises(ValueError, match="outside the allowed catalog"):
        await classify_existing_category_with_llm(
            client,
            "llm",
            filename="Designing Web Interfaces.epub",
            text="web interface design patterns and rich interaction",
            categories=[{"name":"Computing","description":"Computer science and software books"}],
            sample_chars=900,
            timeout=5,
        )


@pytest.mark.asyncio
async def test_designing_web_interfaces_can_be_zero_shot_classified_as_computing():
    from app.collection_classifier import classify_existing_category_with_llm

    client = FakeHybridClient(
        [[1, 0]],
        '{"category":"Computing","confidence":0.94,"rationale":"The book is about software and web interface design."}',
    )
    result = await classify_existing_category_with_llm(
        client,
        "llm",
        filename="Designing Web Interfaces.epub",
        text="principles and patterns for rich interactions, web applications, UI design, software behavior",
        categories=[{
            "name":"Computing",
            "description":"Technical books covering computer science, software engineering, programming, architecture, databases, AI, networking, and related computing disciplines.",
        }],
        sample_chars=900,
        timeout=5,
    )
    assert result["category"] == "Computing"
    assert result["confidence"] == pytest.approx(0.94)
