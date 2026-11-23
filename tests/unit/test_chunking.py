import pytest

from app.chunking import ConfigError, chunk_segments, estimate_tokens, validate_ingest_config
from app.extraction import Segment


LIMITS = {
    "chunk_size_range": [100, 1000],
    "chunk_overlap_range": [0, 500],
    "min_chunk_size_range": [0, 500],
}


def cfg(**overrides):
    base = {
        "strategy": "recursive",
        "chunk_size": 200,
        "chunk_overlap": 20,
        "min_chunk_size": 0,
        "separators": ["\n\n", "\n", ". ", " "],
    }
    base.update(overrides)
    return base


def test_validate_rejects_unknown_strategy():
    with pytest.raises(ConfigError):
        validate_ingest_config(cfg(strategy="magic"), LIMITS)


def test_validate_rejects_overlap_equal_to_size():
    with pytest.raises(ConfigError):
        validate_ingest_config(cfg(chunk_size=200, chunk_overlap=200), LIMITS)


def test_validate_clamps_overlap_to_half_size():
    out = validate_ingest_config(cfg(chunk_size=400, chunk_overlap=300), LIMITS)
    assert out["chunk_overlap"] == 200


def test_fixed_chunking_preserves_sequence_and_bounds():
    text = "abcdefghijklmnopqrstuvwxyz" * 20
    chunks = chunk_segments([Segment(text, {"page": 1})], cfg(strategy="fixed", chunk_size=120, chunk_overlap=20))
    assert len(chunks) > 1
    assert [c.seq for c in chunks] == list(range(len(chunks)))
    assert all(len(c.text) <= 120 for c in chunks)
    assert all(c.loc["page"] == 1 for c in chunks)


def test_markdown_strategy_keeps_headings_separate():
    segments = [
        Segment("A" * 80, {"heading": "One"}),
        Segment("B" * 80, {"heading": "Two"}),
    ]
    chunks = chunk_segments(segments, cfg(strategy="markdown", chunk_size=200))
    assert len(chunks) == 2
    assert chunks[0].loc["heading"] == "One"
    assert chunks[1].loc["heading"] == "Two"


def test_pages_strategy_preserves_page_reference():
    segments = [
        Segment("page one " * 20, {"page": 1}),
        Segment("page two " * 20, {"page": 2}),
    ]
    chunks = chunk_segments(segments, cfg(strategy="pages", chunk_size=200))
    assert {c.loc["page"] for c in chunks} == {1, 2}


def test_estimate_tokens_rounds_up_by_four_chars():
    assert estimate_tokens("12345") == 2
