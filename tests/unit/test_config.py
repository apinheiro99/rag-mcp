from pathlib import Path

from app.config import deep_merge, load_defaults


def test_deep_merge_recursive_and_non_mutating():
    base = {"a": 1, "nested": {"x": 1, "y": 2}}
    override = {"nested": {"y": 9, "z": 3}}
    out = deep_merge(base, override)

    assert out == {"a": 1, "nested": {"x": 1, "y": 9, "z": 3}}
    assert base == {"a": 1, "nested": {"x": 1, "y": 2}}


def test_load_defaults_has_required_sections():
    cfg = load_defaults(Path("config/defaults.yaml"))
    for key in ("ingest", "search", "auto", "limits", "worker", "workflow", "quality", "allowed_extensions", "repository_corpus"):
        assert key in cfg
