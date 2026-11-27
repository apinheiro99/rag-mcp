from pathlib import Path

from app.config import Env, deep_merge, load_defaults


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


def test_env_dsn_requires_explicit_password_without_rag_dsn(monkeypatch):
    monkeypatch.delenv("RAG_DSN", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    env = Env()
    try:
        env.dsn
    except RuntimeError as exc:
        assert "POSTGRES_PASSWORD must be set" in str(exc)
    else:
        raise AssertionError("dsn must not use a hardcoded password fallback")


def test_env_dsn_accepts_explicit_password(monkeypatch):
    monkeypatch.delenv("RAG_DSN", raising=False)
    test_password = "unit-" + "test-" + "db-pass"
    monkeypatch.setenv("POSTGRES_PASSWORD", test_password)
    dsn = Env().dsn
    assert f"password={test_password}" in dsn


def test_env_dsn_explicit_rag_dsn_bypasses_component_password(monkeypatch):
    explicit = "postgresql://user:pass@db/rag"
    monkeypatch.setenv("RAG_DSN", explicit)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    assert Env().dsn == explicit
