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


def test_repository_commit_reads_current_git_revision(tmp_path):
    import subprocess
    from app.source_sync import repository_commit

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)

    expected = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    assert repository_commit(tmp_path) == expected
