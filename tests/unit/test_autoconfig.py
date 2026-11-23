import pytest

from app.autoconfig import _parse_llm_json, build_stats, heuristic_suggestion


DEFAULTS = {
    "ingest": {
        "strategy": "recursive",
        "chunk_size": 1500,
        "chunk_overlap": 200,
        "min_chunk_size": 120,
        "separators": ["\n\n", "\n", ". ", " "],
    }
}
LIMITS = {
    "chunk_size_range": [200, 8000],
    "chunk_overlap_range": [0, 1000],
    "min_chunk_size_range": [0, 2000],
}


def test_parse_llm_json_accepts_fenced_json():
    out = _parse_llm_json('''```json
{"strategy":"recursive","chunk_size":1000}
```''')
    assert out["chunk_size"] == 1000


def test_parse_llm_json_extracts_object_from_surrounding_text():
    out = _parse_llm_json('prefix {"strategy":"fixed","chunk_size":900} suffix')
    assert out["strategy"] == "fixed"


def test_build_stats_detects_markdown_and_sizes():
    stats = build_stats([{
        "filename": "x.md",
        "meta": {"kind": "markdown"},
        "text": "# A\ntext\n\n## B\nmore",
        "size_bytes": 42,
    }])
    assert stats["documents"] == 1
    assert stats["markdown_headings_in_sample"] == 2
    assert stats["total_file_bytes"] == 42


def test_heuristic_prefers_pages_when_available():
    stats = {"kinds": ["pdf"], "has_pages": True, "has_chapters": False, "avg_paragraph_chars": 500}
    out = heuristic_suggestion(stats, DEFAULTS, LIMITS)
    assert out["strategy"] == "pages"


def test_heuristic_prefers_markdown_structure():
    stats = {"kinds": ["markdown"], "has_pages": False, "has_chapters": False, "avg_paragraph_chars": 500}
    out = heuristic_suggestion(stats, DEFAULTS, LIMITS)
    assert out["strategy"] == "markdown"
