#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

info()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }
fail()  { printf '\033[1;31m[erro]\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "Docker não encontrado."
docker info >/dev/null 2>&1 || fail "Docker daemon indisponível ou usuário sem permissão."

if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  fail "Docker Compose não encontrado."
fi

if [ ! -f .env ]; then
  cp .env.example .env
  info ".env criado a partir de .env.example"
fi

if grep -q '^POSTGRES_PASSWORD=change-me-strong-password$' .env; then
  PASS="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)"
  sed -i "s|^POSTGRES_PASSWORD=change-me-strong-password$|POSTGRES_PASSWORD=${PASS}|" .env
  ok "Senha do PostgreSQL gerada."
fi

PRIVATE_PATH="$(grep -E '^RAG_PRIVATE_PATH=' .env | tail -1 | cut -d= -f2-)"
PRIVATE_PATH="${PRIVATE_PATH:-../rag-private}"
mkdir -p "${PRIVATE_PATH}/data/blobs" "${PRIVATE_PATH}/data/extracted" "${PRIVATE_PATH}/data/tmp" "${PRIVATE_PATH}/postgres"
[ -w "${PRIVATE_PATH}" ] || fail "RAG_PRIVATE_PATH não é gravável: ${PRIVATE_PATH}"
ok "Persistência privada: ${PRIVATE_PATH}"

info "Construindo e subindo a stack..."
$COMPOSE up -d --build

PORT="$(grep -E '^RAG_PORT=' .env | tail -1 | cut -d= -f2-)"
PORT="${PORT:-8000}"
info "Aguardando http://localhost:${PORT}/api/health"

for _ in $(seq 1 60); do
  if curl -sf "http://localhost:${PORT}/api/health" >/dev/null 2>&1      || python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/api/health', timeout=3)" >/dev/null 2>&1; then
    ok "RAG MCP no ar."
    echo "Interface: http://localhost:${PORT}/"
    echo "API:       http://localhost:${PORT}/docs"
    echo "MCP:       http://localhost:${PORT}/mcp"
    exit 0
  fi
  sleep 2
done

fail "Servidor não respondeu. Rode: $COMPOSE logs -f app"
