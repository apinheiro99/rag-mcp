#!/usr/bin/env bash
set -euo pipefail

ROOT="${RAG_PROD_ROOT:-$HOME/rag-prod/rag-mcp}"
: "${RAG_PRIVATE_PATH:?RAG_PRIVATE_PATH must point to the durable production private root}"
PRIVATE="$(realpath -m "$RAG_PRIVATE_PATH")"
RUNTIME_ENV="$PRIVATE/.env"

mkdir -p "$(dirname "$ROOT")"
if [ ! -d "$ROOT/.git" ]; then
  git clone "$CI_REPOSITORY_URL" "$ROOT"
fi
git -C "$ROOT" remote set-url origin "$CI_REPOSITORY_URL"
git -C "$ROOT" fetch origin "$CI_COMMIT_SHA"
git -C "$ROOT" checkout --detach "$CI_COMMIT_SHA"

# Mirrors REACH: deployment owns one persistent host root. Existing roots are
# reused; missing roots are created once. Deployment never deletes/replaces it.
if [ ! -e "$PRIVATE" ]; then
  install -d -m 700 "$PRIVATE"
fi
[ -d "$PRIVATE" ] || { echo "RAG_PRIVATE_PATH is not a directory: $PRIVATE" >&2; exit 1; }
[ -w "$PRIVATE" ] || { echo "RAG_PRIVATE_PATH is not writable: $PRIVATE" >&2; exit 1; }

# Existing persistent directories may be owned by the containers that use them
# (not by the gitlab-runner account). Preserve ownership/permissions exactly as-is.
# Only create a directory when it does not exist yet.
for persistent_dir in "$PRIVATE/data" "$PRIVATE/postgres"; do
  if [ ! -e "$persistent_dir" ]; then
    mkdir "$persistent_dir"
  fi
  [ -d "$persistent_dir" ] || { echo "persistent path is not a directory: $persistent_dir" >&2; exit 1; }
done

# Empty-root first deployment: create only infrastructure bootstrap values.
# Installation-specific state subsequently remains in this root across deploys.
if [ ! -f "$RUNTIME_ENV" ]; then
  umask 077
  db_password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')"
  api_token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')"
  {
    printf '%s=%s\n' RAG_PRIVATE_PATH "$PRIVATE"
    printf '%s=%s\n' RAG_PORT "${RAG_PORT:-8000}"
    printf '%s=%s\n' POSTGRES_DB "${POSTGRES_DB:-rag}"
    printf '%s=%s\n' POSTGRES_USER "${POSTGRES_USER:-rag}"
    printf '%s=%s\n' POSTGRES_PASSWORD "$db_password"
    printf '%s=%s\n' LMSTUDIO_URL "${LMSTUDIO_URL:-http://host.docker.internal:1234}"
    printf '%s=%s\n' RAG_API_TOKEN "$api_token"
    printf '%s=%s\n' MCP_ENABLED "${MCP_ENABLED:-true}"
    printf '%s=%s\n' MCP_NAME "${MCP_NAME:-rag-server}"
    printf '%s=%s\n' MCP_ALLOWED_HOSTS "${MCP_ALLOWED_HOSTS:-}"
    printf '%s=%s\n' LOG_LEVEL "${LOG_LEVEL:-INFO}"
  } > "$RUNTIME_ENV"
  echo "created first-run runtime environment at $RUNTIME_ENV"
fi

# The path is deployment policy; ensure an old env cannot point Compose elsewhere.
if grep -q '^RAG_PRIVATE_PATH=' "$RUNTIME_ENV"; then
  sed -i "s|^RAG_PRIVATE_PATH=.*$|RAG_PRIVATE_PATH=$PRIVATE|" "$RUNTIME_ENV"
else
  printf '\nRAG_PRIVATE_PATH=%s\n' "$PRIVATE" >> "$RUNTIME_ENV"
fi

set -a
# shellcheck disable=SC1090
. "$RUNTIME_ENV"
set +a

cd "$ROOT"
export RAG_BUILD_VERSION="${RAG_BUILD_VERSION:-v$(date -u -d "$CI_PIPELINE_CREATED_AT" +%Y.%m).$(printf "%06d" "$CI_PIPELINE_IID")}"
export RAG_BUILD_COMMIT="${RAG_BUILD_COMMIT:-$CI_COMMIT_SHA}"
COMPOSE_ARGS=(-p rag-mcp -f deploy/prod/compose.yml)
if [ -n "${RAG_TUNNEL_BINARY:-}" ] || [ -n "${RAG_TUNNEL_DIR:-}" ]; then
  : "${RAG_TUNNEL_BINARY:?RAG_TUNNEL_BINARY must be set when Secure MCP Tunnel is enabled}"
  : "${RAG_TUNNEL_DIR:?RAG_TUNNEL_DIR must be set when Secure MCP Tunnel is enabled}"
  [ -f "$RAG_TUNNEL_BINARY" ] || { echo "missing tunnel client binary: $RAG_TUNNEL_BINARY" >&2; exit 1; }
  # Mirrors REACH: the tunnel directory is operator-provisioned once and owned by the container uid (1000, mode 700), so the
  # runner can only check the directory itself, never read inside it; the container reports a bad config through its own health.
  [ -d "$RAG_TUNNEL_DIR" ] || { echo "missing tunnel directory: $RAG_TUNNEL_DIR" >&2; exit 1; }
  COMPOSE_ARGS+=(-f deploy/tunnel.compose.yml)
fi

docker compose --env-file "$RUNTIME_ENV" "${COMPOSE_ARGS[@]}" up -d --build

HEALTH_URL="http://127.0.0.1:${RAG_PORT:-8000}/api/health"
APP_READY=false
for attempt in $(seq 1 30); do
  health_json="$(curl -fsS "$HEALTH_URL" 2>/dev/null || true)"
  if [ -n "$health_json" ]; then
    reported_commit="$(printf '%s' "$health_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("build",{}).get("commit",""))' 2>/dev/null || true)"
    reported_status="$(printf '%s' "$health_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' 2>/dev/null || true)"
    if [ "$reported_status" = "ok" ] && [ "$reported_commit" = "$CI_COMMIT_SHA" ]; then
      APP_READY=true
      break
    fi
  fi
  sleep 2
done
[ "$APP_READY" = true ] || { echo "application readiness failed: $HEALTH_URL did not report commit $CI_COMMIT_SHA" >&2; exit 1; }

if [ -n "${RAG_TUNNEL_BINARY:-}" ] && [ -n "${RAG_TUNNEL_DIR:-}" ]; then
  TUNNEL_READY=false
  for attempt in $(seq 1 30); do
    if docker exec "${RAG_TUNNEL_CONTAINER_NAME:-rag-mcp-tunnel}"          /usr/local/bin/tunnel-client health --port 8080 --require-control-plane-poll >/dev/null 2>&1; then
      TUNNEL_READY=true
      break
    fi
    sleep 2
  done
  [ "$TUNNEL_READY" = true ] || { echo "secure MCP tunnel did not become ready" >&2; exit 1; }
fi

EVAL_ARGS=(
  python /srv/rag/scripts/evaluate_retrieval.py
  --url http://127.0.0.1:8000/mcp
  --cases /srv/rag/examples/evaluation/retrieval.json
  --output /tmp/rag-retrieval-eval.json
  --k 5 --min-recall 0.80 --min-mrr 0.60 --max-avg-ms 1500
)
if [ -n "${RAG_API_TOKEN:-}" ]; then
  EVAL_ARGS+=(--bearer-token "$RAG_API_TOKEN")
fi
docker compose --env-file "$RUNTIME_ENV" "${COMPOSE_ARGS[@]}" exec -T app "${EVAL_ARGS[@]}"
