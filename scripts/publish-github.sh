#!/usr/bin/env bash
set -euo pipefail

: "${RAG_PRIVATE_REPOSITORY:=https://gitlab-ci-token:${CI_JOB_TOKEN}@${CI_SERVER_HOST}/rag/rag-private.git}"
PRIVATE_DIR="$(mktemp -d)"
trap 'rm -rf "$PRIVATE_DIR"' EXIT

if [ -n "${CI_SERVER_TLS_CA_FILE:-}" ]; then
  export GIT_SSL_CAINFO="$CI_SERVER_TLS_CA_FILE"
fi

git clone --depth 1 "$RAG_PRIVATE_REPOSITORY" "$PRIVATE_DIR"
KEY="$PRIVATE_DIR/credentials/github/rag-mcp"
CONFIG="$PRIVATE_DIR/config/github.env"
[ -f "$KEY" ] || { echo "missing GitHub deploy key in rag-private: credentials/github/rag-mcp" >&2; exit 1; }
[ -f "$CONFIG" ] || { echo "missing GitHub config in rag-private: config/github.env" >&2; exit 1; }
# shellcheck disable=SC1090
. "$CONFIG"
: "${RAG_GITHUB_OWNER:?RAG_GITHUB_OWNER must be set in rag-private/config/github.env}"
: "${RAG_GITHUB_REPO:=rag-mcp}"
: "${RAG_GITHUB_REPOSITORY:=git@github.com:${RAG_GITHUB_OWNER}/${RAG_GITHUB_REPO}.git}"

if [ -n "${RAG_GITHUB_TOKEN:-}" ]; then
  api="https://api.github.com/repos/${RAG_GITHUB_OWNER}/${RAG_GITHUB_REPO}"
  if ! curl -fsS -H "Authorization: Bearer ${RAG_GITHUB_TOKEN}" -H "Accept: application/vnd.github+json" "$api" >/dev/null; then
    curl -fsS -X POST \
      -H "Authorization: Bearer ${RAG_GITHUB_TOKEN}" \
      -H "Accept: application/vnd.github+json" \
      https://api.github.com/user/repos \
      -d "{\"name\":\"${RAG_GITHUB_REPO}\",\"private\":false,\"description\":\"Self-hosted RAG server with MCP, pgvector, reranking and automated deployment\"}" >/dev/null
  fi
fi

chmod 600 "$KEY"
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
TAG="v$(date -u -d "$CI_PIPELINE_CREATED_AT" +%Y.%m).$(printf "%06d" "$CI_PIPELINE_IID")"

git remote remove github 2>/dev/null || true
git remote add github "$RAG_GITHUB_REPOSITORY"
git push --force-with-lease github "$CI_COMMIT_SHA:refs/heads/main"
git push github "refs/tags/$TAG:refs/tags/$TAG"
