from pathlib import Path
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


def test_source_sync_change_is_explicitly_transactional():
    from pathlib import Path
    source = Path("app/source_sync.py").read_text()
    assert "async with conn.transaction():" in source
    delete = source.index('DELETE FROM chunks WHERE document_id=%s')
    update = source.index('UPDATE documents SET filename=%s', delete)
    transaction = source.rfind('async with conn.transaction():', 0, delete)
    assert transaction < delete < update


def test_source_sync_skips_deletion_pass_after_scan_error_and_gcs_old_blobs():
    source = Path("app/source_sync.py").read_text()
    assert "scan_complete = True" in source
    assert "scan_complete = False" in source
    assert "if scan_complete:" in source
    assert "source sync deletion pass skipped because filesystem scan had errors" in source
    assert 'obsolete_blobs.append(str(old["stored_path"]))' in source
    assert "await _gc_blobs(obsolete_blobs)" in source
    assert "SELECT DISTINCT stored_path FROM documents WHERE stored_path = ANY(%s)" in source


def test_source_sync_defers_active_documents_and_serializes_same_source():
    source = Path("app/source_sync.py").read_text()
    assert 'if old and old["status"] in ACTIVE_STATUSES:' in source
    assert 'if old["status"] in ACTIVE_STATUSES:' in source
    assert '_source_sync_locks: dict[str, asyncio.Lock] = {}' in source
    assert 'async with lock:' in source
    assert 'return await _sync_collection_source_unlocked(source_id)' in source
