#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  migrate-rag-data.sh export <bundle.tar.gz> [compose-file] [env-file]
  migrate-rag-data.sh import <bundle.tar.gz> [compose-file] [env-file]

Export creates a portable logical backup: PostgreSQL pg_dump + RAG data/.
Import restores into RAG_PRIVATE_PATH and never copies a live PostgreSQL data directory.
EOF
}

command="${1:-}"
bundle="${2:-}"
[ -n "$command" ] && [ -n "$bundle" ] || { usage >&2; exit 2; }

case "$command" in
  export)
    compose="${3:-docker-compose.yml}"
    env_file="${4:-.env}"
    [ -f "$compose" ] || { echo "compose file not found: $compose" >&2; exit 1; }
    [ -f "$env_file" ] || { echo "env file not found: $env_file" >&2; exit 1; }
    set -a; . "$env_file"; set +a
    : "${RAG_PRIVATE_PATH:?RAG_PRIVATE_PATH missing from env}"
    root="$(realpath -m "$RAG_PRIVATE_PATH")"
    [ -d "$root/data" ] || { echo "source data directory not found: $root/data" >&2; exit 1; }
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
    mkdir -p "$tmp/payload"
    echo "exporting PostgreSQL logically..."
    docker compose --env-file "$env_file" -f "$compose" exec -T db \
      pg_dump -U "${POSTGRES_USER:-rag}" -d "${POSTGRES_DB:-rag}" -Fc > "$tmp/payload/database.dump"
    echo "copying RAG data blobs/cache..."
    cp -a "$root/data" "$tmp/payload/data"
    python3 - <<PY > "$tmp/payload/manifest.json"
import json, datetime, pathlib, hashlib
p=pathlib.Path("$tmp/payload/database.dump")
print(json.dumps({
  "schema": 1,
  "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
  "source_private_path": "$root",
  "database_sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
}, indent=2))
PY
    tar -C "$tmp/payload" -czf "$bundle" .
    echo "bundle created: $bundle"
    ;;

  import)
    compose="${3:-deploy/prod/compose.yml}"
    : "${RAG_PRIVATE_PATH:?set RAG_PRIVATE_PATH to the production persistent root}"
    root="$(realpath -m "$RAG_PRIVATE_PATH")"
    env_file="${4:-$root/.env}"
    [ -f "$bundle" ] || { echo "bundle not found: $bundle" >&2; exit 1; }
    [ -f "$compose" ] || { echo "compose file not found: $compose" >&2; exit 1; }
    [ -f "$env_file" ] || { echo "runtime env not found: $env_file" >&2; exit 1; }
    set -a; . "$env_file"; set +a
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
    tar -C "$tmp" -xzf "$bundle"
    [ -f "$tmp/database.dump" ] || { echo "bundle missing database.dump" >&2; exit 1; }
    [ -d "$tmp/data" ] || { echo "bundle missing data/" >&2; exit 1; }

    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    backup="$root/pre-migration-$stamp"
    mkdir -p "$root" "$backup"

    echo "stopping RAG application for consistent restore..."
    docker compose --env-file "$env_file" -f "$compose" stop app || true

    if [ -d "$root/data" ] && [ "$(find "$root/data" -mindepth 1 -print -quit 2>/dev/null)" ]; then
      mv "$root/data" "$backup/data"
    fi
    cp -a "$tmp/data" "$root/data"

    echo "starting PostgreSQL for logical restore..."
    docker compose --env-file "$env_file" -f "$compose" up -d db
    for _ in $(seq 1 30); do
      if docker compose --env-file "$env_file" -f "$compose" exec -T db \
          pg_isready -U "${POSTGRES_USER:-rag}" -d "${POSTGRES_DB:-rag}" >/dev/null 2>&1; then break; fi
      sleep 2
    done
    docker compose --env-file "$env_file" -f "$compose" exec -T db \
      psql -U "${POSTGRES_USER:-rag}" -d postgres -v ON_ERROR_STOP=1 \
      -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${POSTGRES_DB:-rag}' AND pid <> pg_backend_pid();" >/dev/null
    cat "$tmp/database.dump" | docker compose --env-file "$env_file" -f "$compose" exec -T db \
      pg_restore --clean --if-exists --no-owner -U "${POSTGRES_USER:-rag}" -d "${POSTGRES_DB:-rag}"
    docker compose --env-file "$env_file" -f "$compose" up -d --build
    echo "migration restored; previous data backup (if any): $backup"
    ;;

  *) usage >&2; exit 2 ;;
esac
