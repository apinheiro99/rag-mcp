#!/usr/bin/env bash
set -euo pipefail

# Publication credentials are operator-owned host material. This job never clones,
# replaces or mutates the production RAG private root.
PUBLICATION_ROOT="${RAG_PUBLICATION_PATH:-$HOME/rag-prod/rag-private}"
KEY="$PUBLICATION_ROOT/credentials/github/rag-mcp"
CONFIG="$PUBLICATION_ROOT/config/github.env"
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
git push --force github "$CI_COMMIT_SHA:refs/heads/main"
git push github "refs/tags/$TAG:refs/tags/$TAG"

REMOTE_MAIN="$(git ls-remote github refs/heads/main | awk '{print $1}')"
REMOTE_TAG="$(git ls-remote github "refs/tags/$TAG" | awk '{print $1}')"
[ "$REMOTE_MAIN" = "$CI_COMMIT_SHA" ] || { echo "GitHub main SHA mismatch: expected $CI_COMMIT_SHA got $REMOTE_MAIN" >&2; exit 1; }
[ "$REMOTE_TAG" = "$CI_COMMIT_SHA" ] || { echo "GitHub tag SHA mismatch: expected $CI_COMMIT_SHA got $REMOTE_TAG" >&2; exit 1; }
echo "GitHub publication verified: $CI_COMMIT_SHA ($TAG)"
