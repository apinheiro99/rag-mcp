from app.api import _safe_relpath


def test_safe_relpath_removes_parent_traversal():
    assert _safe_relpath("../../secret/file.txt", "fallback.txt") == "secret/file.txt"


def test_safe_relpath_normalizes_windows_separators():
    assert _safe_relpath(r"folder\sub\file.txt", "fallback.txt") == "folder/sub/file.txt"


def test_safe_relpath_uses_fallback_when_empty():
    assert _safe_relpath("", "fallback.txt") == "fallback.txt"
