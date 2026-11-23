#!/usr/bin/env bash
set -euo pipefail
ROOT="${RAG_PROD_ROOT:-$HOME/rag-prod/rag-mcp}"
PRIVATE="${RAG_PRIVATE_PATH:-$HOME/rag-prod/rag-private}"
mkdir -p "$(dirname "$ROOT")"
if [ ! -d "$ROOT/.git" ]; then
  git clone "$CI_REPOSITORY_URL" "$ROOT"
fi
git -C "$ROOT" fetch origin "$CI_COMMIT_SHA"
git -C "$ROOT" checkout --detach "$CI_COMMIT_SHA"
[ -f "$PRIVATE/.env" ] || { echo "missing $PRIVATE/.env" >&2; exit 1; }
cp "$PRIVATE/.env" "$ROOT/.env"
cd "$ROOT"
docker compose up -d --build
curl -fsS "http://127.0.0.1:${RAG_PORT:-8000}/api/health" >/dev/null
