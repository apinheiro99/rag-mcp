import pytest
from app.source_sync import is_excluded, validate_interval


def test_validate_interval_uses_configured_options_only():
    cfg = {"interval_options_seconds": [600, 21600, 43200, 86400]}
    assert validate_interval(21600, cfg) == 21600
    with pytest.raises(ValueError, match="interval must be one of"):
        validate_interval(123, cfg)


def test_is_excluded_supports_tree_and_glob_patterns():
    patterns = [".git/**", "node_modules/**", "*.tmp"]
    assert is_excluded("project/.git/config", patterns)
    assert is_excluded("node_modules/pkg/index.js", patterns)
    assert is_excluded("scratch.tmp", patterns)
    assert not is_excluded("src/app.py", patterns)
