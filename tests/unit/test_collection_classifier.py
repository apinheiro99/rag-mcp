import pytest

from app.collection_classifier import parse_json_object, representative_sample, validate_classification

COLLECTIONS = [
    {"name": "Python", "description": "Python books", "doc_count": 4},
    {"name": "RAG & AI Agents", "description": "RAG and agents", "doc_count": 7},
]


def test_parse_json_object_accepts_fenced_json():
    assert parse_json_object('```json\n{"confidence": 0.9}\n```')["confidence"] == 0.9


def test_validate_existing_collection():
    result = validate_classification({
        "existing_collection": "RAG & AI Agents",
        "confidence": 0.94,
        "proposed_collection": {"name": "ignored"},
        "rationale": "RAG content",
    }, COLLECTIONS)
    assert result["existing_collection"] == "RAG & AI Agents"
    assert result["proposed_collection"] is None
    assert result["confidence"] == 0.94


def test_validate_rejects_unknown_existing_collection():
    with pytest.raises(ValueError, match="unknown collection"):
        validate_classification({"existing_collection": "Magic", "confidence": 0.8}, COLLECTIONS)


def test_validate_promotes_duplicate_proposal_to_existing():
    result = validate_classification({
        "existing_collection": None,
        "confidence": 0.72,
        "proposed_collection": {"name": "Python", "description": "duplicate"},
    }, COLLECTIONS)
    assert result["existing_collection"] == "Python"
    assert result["proposed_collection"] is None


def test_representative_sample_spreads_across_document():
    text = "BEGIN_MARKER " + ("a" * 3000) + " MIDDLE_MARKER " + ("b" * 3000) + " END_MARKER"
    sample = representative_sample(text, 900)
    assert "BEGIN_MARKER" in sample
    assert "MIDDLE_MARKER" in sample
    assert "END_MARKER" in sample
    assert "[BEGIN]" in sample and "[MIDDLE]" in sample and "[END]" in sample


def test_representative_sample_returns_short_text_unchanged():
    assert representative_sample("short content", 900) == "short content"
