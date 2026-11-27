"""Configuração central do RAG Server.

Três camadas, da mais fraca para a mais forte:
  1. config/defaults.yaml  — defaults de processamento (versionados)
  2. settings no PostgreSQL — overrides feitos pela UI em runtime
  3. variáveis de ambiente / .env — infraestrutura (portas, DSN, paths)

Nenhum parâmetro importante é hardcoded nos módulos.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml
from psycopg.conninfo import make_conninfo

BASE_DIR = Path(__file__).resolve().parent.parent


def _default_int(section: str, key: str, fallback: int) -> int:
    try:
        data = yaml.safe_load((BASE_DIR / "config" / "defaults.yaml").read_text(encoding="utf-8")) or {}
        return int(data.get(section, {}).get(key, fallback))
    except Exception:
        return fallback


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    fallback = "true" if default else "false"
    return _env(name, fallback).lower() in ("1", "true", "yes", "on")


class Env:
    """Configuração de infraestrutura vinda do ambiente/.env."""

    def __init__(self) -> None:
        self.app_host = _env("RAG_HOST", "0.0.0.0")
        self.app_port = _env_int("RAG_APP_PORT", 8000)
        self.data_dir = Path(_env("DATA_DIR", str(BASE_DIR / "data")))
        self.defaults_file = Path(_env("DEFAULTS_FILE", str(BASE_DIR / "config" / "defaults.yaml")))
        self.api_token = _env("RAG_API_TOKEN", "")
        self.mcp_enabled = _env_bool("MCP_ENABLED", True)
        self.mcp_name = _env("MCP_NAME", "rag-server")
        self.mcp_allowed_hosts = [h.strip() for h in _env("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
        self.log_level = _env("LOG_LEVEL", "INFO").upper()
        self.build_version = _env("RAG_BUILD_VERSION", "dev")
        self.build_commit = _env("RAG_BUILD_COMMIT", "")
        self.external_parser_timeout = _env_int("RAG_EXTERNAL_PARSER_TIMEOUT", _default_int("ingest", "external_parser_timeout", 60))
        self.lmstudio_url_default = _env("LMSTUDIO_URL", "http://127.0.0.1:1234").rstrip("/")

    @property
    def dsn(self) -> str:
        explicit = _env("RAG_DSN", "")
        if explicit:
            return explicit
        host = _env("POSTGRES_HOST", "127.0.0.1")
        port = _env_int("POSTGRES_PORT", 5432)
        db = _env("POSTGRES_DB", "rag")
        user = _env("POSTGRES_USER", "rag")
        password = _env("POSTGRES_PASSWORD", "")
        if not password:
            raise RuntimeError("POSTGRES_PASSWORD must be set when RAG_DSN is not configured")
        return make_conninfo(host=host, port=port, dbname=db, user=user, password=password)

    @property
    def blobs_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def extracted_dir(self) -> Path:
        return self.data_dir / "extracted"


def deep_merge(base: dict, override: dict) -> dict:
    """Merge recursivo: valores de `override` vencem; dicts são fundidos."""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_defaults(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"defaults file {path} must contain a mapping")
    return data


env = Env()
