#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

log() { printf '\033[1;34m[postgres-dev]\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[erro]\033[0m %s\n' "$*" >&2; exit 1; }

PGVECTOR_IMAGE="pgvector/pgvector:0.8.7-pg19"
export PGVECTOR_IMAGE

# ------------------------------------------------------------
# 1. Docker / Compose
# ------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  log "Docker não encontrado. Instalando..."
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl gnupg docker.io
  sudo systemctl enable --now docker 2>/dev/null || true
fi

if ! docker info >/dev/null 2>&1; then
  if sudo docker info >/dev/null 2>&1; then
    sudo usermod -aG docker "$USER" || true
    die "Usuário adicionado ao grupo docker. Reabra a sessão ou rode 'newgrp docker' e execute novamente."
  fi
  die "Docker daemon não está acessível."
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  log "Docker Compose não encontrado. Instalando..."
  sudo apt-get update
  sudo apt-get install -y docker-compose-plugin 2>/dev/null || sudo apt-get install -y docker-compose

  if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
  else
    die "Não foi possível instalar Docker Compose."
  fi
fi

ok "Docker e Compose disponíveis."

# ------------------------------------------------------------
# 2. Environment / private storage
# ------------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  log ".env criado a partir de .env.example"
fi

if grep -q '^POSTGRES_PASSWORD=change-me-strong-password$' .env; then
  PASS="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 28)"
  sed -i "s|^POSTGRES_PASSWORD=change-me-strong-password$|POSTGRES_PASSWORD=${PASS}|" .env
  ok "Senha do PostgreSQL gerada automaticamente."
fi

PRIVATE_PATH="$(grep -E '^RAG_PRIVATE_PATH=' .env | tail -1 | cut -d= -f2- || true)"
PRIVATE_PATH="${PRIVATE_PATH:-../rag-private}"

DB_USER="$(grep -E '^POSTGRES_USER=' .env | tail -1 | cut -d= -f2- || true)"
DB_USER="${DB_USER:-rag}"

DB_NAME="$(grep -E '^POSTGRES_DB=' .env | tail -1 | cut -d= -f2- || true)"
DB_NAME="${DB_NAME:-rag}"

mkdir -p   "${PRIVATE_PATH}/data/blobs"   "${PRIVATE_PATH}/data/extracted"   "${PRIVATE_PATH}/data/tmp"   "${PRIVATE_PATH}/postgres"

[ -w "${PRIVATE_PATH}" ] || die "RAG_PRIVATE_PATH não é gravável: ${PRIVATE_PATH}"

# ------------------------------------------------------------
# 3. Garantir imagem ANTES de qualquer operação destrutiva
# ------------------------------------------------------------
log "Garantindo imagem canônica: $PGVECTOR_IMAGE"
PULL_OK=0
for attempt in 1 2 3; do
  if docker pull "$PGVECTOR_IMAGE"; then
    PULL_OK=1
    break
  fi
  log "Falha ao baixar imagem (tentativa $attempt/3)."
  sleep $((attempt * 3))
done

if [ "$PULL_OK" -ne 1 ]; then
  if docker image inspect "$PGVECTOR_IMAGE" >/dev/null 2>&1; then
    log "Docker Hub indisponível, mas a imagem canônica já existe localmente. Continuando."
  else
    die "Não foi possível baixar $PGVECTOR_IMAGE e não há cópia local. Nenhum dado foi apagado."
  fi
fi

# ------------------------------------------------------------
# 4. Full DEV reset
# ------------------------------------------------------------
log "Removendo container PostgreSQL DEV atual..."
"${COMPOSE[@]}" stop db >/dev/null 2>&1 || true
"${COMPOSE[@]}" rm -sf db >/dev/null 2>&1 || true

log "Removendo TODOS os dados PostgreSQL DEV anteriores..."
sudo rm -rf "${PRIVATE_PATH}/postgres"
mkdir -p "${PRIVATE_PATH}/postgres"
sudo chown -R "$(id -u):$(id -g)" "${PRIVATE_PATH}/postgres"
chmod 755 "${PRIVATE_PATH}/postgres"

ok "PostgreSQL DEV anterior removido."

# ------------------------------------------------------------
# 5. Fresh PostgreSQL + pgvector
# ------------------------------------------------------------
log "Criando PostgreSQL/pgvector do zero..."
"${COMPOSE[@]}" up -d --force-recreate db

log "Aguardando PostgreSQL inicializar..."
READY=0
for _ in $(seq 1 60); do
  if "${COMPOSE[@]}" exec -T db pg_isready -U "$DB_USER" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 2
done

[ "$READY" -eq 1 ] || {
  "${COMPOSE[@]}" logs --tail=100 db || true
  die "PostgreSQL não ficou pronto."
}

log "Aguardando banco '$DB_NAME' ficar disponível..."
DB_READY=0
for _ in $(seq 1 30); do
  if "${COMPOSE[@]}" exec -T db psql -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT 1" 2>/dev/null | grep -qx 1; then
    DB_READY=1
    break
  fi
  sleep 1
done

if [ "$DB_READY" -ne 1 ]; then
  log "Banco '$DB_NAME' não apareceu automaticamente; criando manualmente..."
  "${COMPOSE[@]}" exec -T db createdb -U "$DB_USER" "$DB_NAME" 2>/dev/null || true
  "${COMPOSE[@]}" exec -T db psql -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT 1" >/dev/null \
    || die "Banco '$DB_NAME' não ficou disponível."
fi

log "Ativando e validando pgvector..."
"${COMPOSE[@]}" exec -T db psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
SELECT version();
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
SQL

ok "PostgreSQL DEV recriado do zero com PostgreSQL 19 + pgvector 0.8.7."
echo
echo "Banco: $DB_NAME"
echo "Usuário: $DB_USER"
echo "Persistência: ${PRIVATE_PATH}/postgres/pgdata"
echo
echo "Comandos úteis:"
echo "  docker compose ps"
echo "  docker compose logs -f db"
echo "  docker compose exec db psql -U $DB_USER -d $DB_NAME"
