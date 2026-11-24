#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
BRIDGE_DIR="${RAG_BRIDGE_DIR:-$ROOT_DIR/../vscode-mcp-bridge}"
HEALTH_TIMEOUT="${RAG_START_HEALTH_TIMEOUT:-90}"
START_BRIDGE="${RAG_START_BRIDGE:-true}"

cd "$ROOT_DIR"

info() { printf '\033[1;34m[start]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[aviso]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[erro]\033[0m %s\n' "$*" >&2; exit 1; }

bool_enabled() {
  case "${1,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
  else
    die "Docker Compose não encontrado. Rode ./install.sh primeiro."
  fi
}

ensure_docker() {
  command -v docker >/dev/null 2>&1 || die "Docker não encontrado. Rode ./install.sh primeiro."
  if docker info >/dev/null 2>&1; then return 0; fi
  if command -v systemctl >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    info "Docker daemon não está ativo; tentando iniciar..."
    sudo -n systemctl start docker || true
  fi
  docker info >/dev/null 2>&1 || die "Docker daemon indisponível ou usuário sem permissão."
}

env_value() {
  local key="$1" default="$2"
  local value=""
  if [ -f .env ]; then
    value="$(grep -E "^${key}=" .env | tail -1 | cut -d= -f2- || true)"
  fi
  printf '%s' "${value:-$default}"
}

wait_rag() {
  local port="$1"
  local url="http://127.0.0.1:${port}/api/health"
  local deadline=$((SECONDS + HEALTH_TIMEOUT))
  info "Aguardando RAG em $url ..."
  while (( SECONDS < deadline )); do
    if command -v curl >/dev/null 2>&1; then
      if curl -sf "$url" >/dev/null 2>&1; then ok "RAG saudável."; return 0; fi
    elif python3 - "$url" <<'PY' >/dev/null 2>&1
import sys, urllib.request
urllib.request.urlopen(sys.argv[1], timeout=3).read()
PY
    then
      ok "RAG saudável."
      return 0
    fi
    sleep 2
  done
  "${COMPOSE[@]}" ps || true
  "${COMPOSE[@]}" logs --tail=80 app || true
  die "RAG não ficou saudável em ${HEALTH_TIMEOUT}s."
}

start_bridge() {
  if ! bool_enabled "$START_BRIDGE"; then
    warn "Bridge/túnel desabilitado por RAG_START_BRIDGE=$START_BRIDGE."
    return 0
  fi
  [ -d "$BRIDGE_DIR" ] || die "Bridge não encontrada: $BRIDGE_DIR"
  [ -f "$BRIDGE_DIR/scripts/vscode-tunnel.sh" ] || die "Controlador da bridge não encontrado: $BRIDGE_DIR/scripts/vscode-tunnel.sh"
  info "Subindo bridge/túnel VS Code MCP..."
  bash "$BRIDGE_DIR/scripts/vscode-tunnel.sh" start
  bash "$BRIDGE_DIR/scripts/vscode-tunnel.sh" status
  ok "Bridge/túnel ativo."
}

show_summary() {
  local port="$1"
  local host public_base
  host="${RAG_PUBLIC_HOST:-$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')}"
  if [ -z "$host" ]; then
    host="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -Ev '^(127\.|172\.(1[6-9]|2[0-9]|3[0-1])\.)' | head -1 || true)"
  fi
  host="${host:-localhost}"
  public_base="${RAG_PUBLIC_URL:-http://${host}:${port}}"
  echo
  echo "================ RAG DEV ================"
  "${COMPOSE[@]}" ps
  echo
  echo "UI:   ${public_base}/"
  echo "API:  ${public_base}/docs"
  echo "MCP:  ${public_base}/mcp"
  if bool_enabled "$START_BRIDGE"; then echo "Bridge: ativa via $BRIDGE_DIR"; fi
  echo "========================================="
}

main() {
  [ -f .env ] || die ".env não encontrado. Rode ./install.sh primeiro."
  ensure_docker
  compose_cmd
  local port
  port="$(env_value RAG_PORT 8000)"
  export RAG_BUILD_VERSION="${RAG_BUILD_VERSION:-$(git describe --tags --always --dirty 2>/dev/null || git rev-parse --short HEAD)}"
  export RAG_BUILD_COMMIT="${RAG_BUILD_COMMIT:-$(git rev-parse HEAD)}"
  info "Subindo PostgreSQL + RAG..."
  if [ "${1:-}" = "--build" ]; then
    "${COMPOSE[@]}" up -d --build
  else
    "${COMPOSE[@]}" up -d
  fi
  wait_rag "$port"
  start_bridge
  show_summary "$port"
  ok "Ambiente DEV inteiro está no ar."
}

main "$@"
