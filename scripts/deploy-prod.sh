#!/usr/bin/env bash
set -euo pipefail
ROOT="${RAG_PROD_ROOT:-$HOME/rag-prod/rag-mcp}"
PRIVATE="${RAG_PRIVATE_PATH:-$HOME/rag-prod/rag-private}"
mkdir -p "$(dirname "$ROOT")"
if [ ! -d "$ROOT/.git" ]; then
  git clone "$CI_REPOSITORY_URL" "$ROOT"
fi
git -C "$ROOT" remote set-url origin "$CI_REPOSITORY_URL"
git -C "$ROOT" fetch origin "$CI_COMMIT_SHA"
git -C "$ROOT" checkout --detach "$CI_COMMIT_SHA"
[ -f "$PRIVATE/.env" ] || { echo "missing $PRIVATE/.env" >&2; exit 1; }
cp "$PRIVATE/.env" "$ROOT/.env"
cd "$ROOT"
COMPOSE_ARGS=(-f docker-compose.yml)
if [ -n "${RAG_TUNNEL_BINARY:-}" ] || [ -n "${RAG_TUNNEL_DIR:-}" ]; then
  : "${RAG_TUNNEL_BINARY:?RAG_TUNNEL_BINARY must be set when Secure MCP Tunnel is enabled}"
  : "${RAG_TUNNEL_DIR:?RAG_TUNNEL_DIR must be set when Secure MCP Tunnel is enabled}"
  [ -f "$RAG_TUNNEL_BINARY" ] || { echo "missing tunnel client binary: $RAG_TUNNEL_BINARY" >&2; exit 1; }
  [ -f "$RAG_TUNNEL_DIR/tunnel-client.yaml" ] || { echo "missing tunnel config: $RAG_TUNNEL_DIR/tunnel-client.yaml" >&2; exit 1; }
  COMPOSE_ARGS+=(-f deploy/tunnel.compose.yml)
fi
docker compose "${COMPOSE_ARGS[@]}" up -d --build
HEALTH_URL="http://127.0.0.1:${RAG_PORT:-8000}/api/health"
for attempt in $(seq 1 30); do
  if curl -fsS "$HEALTH_URL" >/dev/null; then
    exit 0
  fi
  sleep 2
done
echo "health check failed after startup grace period: $HEALTH_URL" >&2
exit 1
