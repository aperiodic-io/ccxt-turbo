#!/usr/bin/env bash
# Merge upstream ccxt/ccxt master into CCXT-turbo.
#
# CCXT-turbo ships Python and Go only, so the other language outputs and their
# CI/release tooling are dropped from every merge, and README.md is always kept
# as ours. Anything else that conflicts is left for you to resolve.
#
# Usage: scripts/sync-upstream.sh   (from a clean checkout of master; push afterwards)

set -euo pipefail

UPSTREAM_URL=https://github.com/ccxt/ccxt.git

REMOVED_PATHS=(
    cs php java rust dist cli mcp
    ccxt.php composer.json composer.lock
    examples/cs examples/java examples/php examples/rust examples/html
    .github/workflows/cs.yml .github/workflows/php.yml .github/workflows/java.yml
    .github/workflows/rust.yml .github/workflows/mcp.yml .github/workflows/mcp-release.yml
    .github/workflows/release.yml .github/workflows/release-short.yml
    .github/workflows/release-js-only.yml .github/workflows/post-release.yml
    .github/workflows/deploy-playground.yml .github/workflows/docs-fumadocs.yml
    .github/workflows/test-env.yml
)

OURS_PATHS=(README.md)

cd "$(git rev-parse --show-toplevel)"

if [ -n "$(git status --porcelain)" ]; then
    echo "working tree is not clean" >&2
    exit 1
fi

git remote get-url upstream >/dev/null 2>&1 || git remote add upstream "$UPSTREAM_URL"
git fetch upstream master

if git merge-base --is-ancestor upstream/master HEAD; then
    echo "already up to date with upstream/master"
    exit 0
fi

git merge --no-ff --no-commit upstream/master || true

for path in "${REMOVED_PATHS[@]}"; do
    git rm -rq --cached --ignore-unmatch -- "$path"
    rm -rf -- "$path"
done

for path in "${OURS_PATHS[@]}"; do
    git checkout HEAD -- "$path"
done

unmerged=$(git diff --name-only --diff-filter=U)
if [ -n "$unmerged" ]; then
    echo "resolve these conflicts, then 'git commit':" >&2
    echo "$unmerged" >&2
    exit 1
fi

git commit -q -m "chore: sync with upstream ccxt/ccxt $(git rev-parse --short upstream/master)"
echo "merged upstream/master; review with 'git log -1 --stat' and push"
