#!/usr/bin/env bash
set -euo pipefail

PRIVATE="${RAG_PRIVATE_PATH:-$HOME/rag-prod/rag-private}"

# One-time bootstrap only when the production-private checkout does not exist yet.
# After bootstrap, publication uses the host-local private path, matching REACH.
if [ ! -d "$PRIVATE/.git" ]; then
  : "${RAG_PRIVATE_BOOTSTRAP_REPOSITORY:=https://gitlab-ci-token:${CI_JOB_TOKEN}@${CI_SERVER_HOST}/rag/rag-private.git}"
  mkdir -p "$(dirname "$PRIVATE")"
  if [ -n "${CI_SERVER_TLS_CA_FILE:-}" ]; then export GIT_SSL_CAINFO="$CI_SERVER_TLS_CA_FILE"; fi
  git clone "$RAG_PRIVATE_BOOTSTRAP_REPOSITORY" "$PRIVATE"
fi

KEY="$PRIVATE/credentials/github/rag-mcp"
CONFIG="$PRIVATE/config/github.env"
[ -f "$KEY" ] || { echo "missing GitHub deploy key: $KEY" >&2; exit 1; }
[ -f "$CONFIG" ] || { echo "missing GitHub config: $CONFIG" >&2; exit 1; }
# shellcheck disable=SC1090
. "$CONFIG"
: "${RAG_GITHUB_OWNER:?RAG_GITHUB_OWNER must be set in $CONFIG}"
: "${RAG_GITHUB_REPO:=rag-mcp}"
: "${RAG_GITHUB_REPOSITORY:=git@github.com:${RAG_GITHUB_OWNER}/${RAG_GITHUB_REPO}.git}"

chmod 600 "$KEY"
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
TAG="v$(date -u -d "$CI_PIPELINE_CREATED_AT" +%Y.%m).$(printf "%06d" "$CI_PIPELINE_IID")"

git remote remove github 2>/dev/null || true
git remote add github "$RAG_GITHUB_REPOSITORY"
git push --force-with-lease github "$CI_COMMIT_SHA:refs/heads/main"
git push github "refs/tags/$TAG:refs/tags/$TAG"
