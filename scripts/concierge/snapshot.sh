#!/usr/bin/env bash
# Concierge snapshot — push the tree to the fork (personal) as the durable off-site copy.
#
# The fork is a SNAPSHOT repo, not a history mirror: a full-history push lazy-fetches
# ~12 GB from the upstream promisor remote and must never happen. Each push is one
# commit-tree of the working tree, parented on the previous snapshot.
#
#   SNAP=$(git commit-tree <tip>^{tree} -p <prev-snapshot> -m "Snapshot …")
#   git push personal "$SNAP":refs/heads/main
#   git update-ref refs/snapshots/<name> "$SNAP"
#
# Usage:
#   scripts/concierge/snapshot.sh "message"   # snapshot main (default)
set -euo pipefail

REPO="${CONCIERGE_REPO:-$HOME/.hermes/hermes-agent}"
MSG="${1:-}"
export GIT_SSH_COMMAND="ssh -i ${HOME}/.ssh/id_ed25519_gh_personal -o BatchMode=yes -o StrictHostKeyChecking=accept-new"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

cd "$REPO"
git rev-parse --git-dir >/dev/null 2>&1 || die "not a git repo: $REPO"

branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$branch" = "main" ] || die "refusing to snapshot from '$branch' — snapshot main."
[ -z "$(git status --porcelain)" ] || die "working tree is dirty; commit first."
[ -z "$MSG" ] && die 'usage: snapshot.sh "message"'

git remote get-url personal >/dev/null 2>&1 || die "no 'personal' remote (the fork)."

PREV="$(git rev-parse personal/main)"
SNAP="$(git commit-tree main^{tree} -p "$PREV" -m "Snapshot $MSG")"

git push personal "$SNAP":refs/heads/main
git update-ref "refs/snapshots/$(date +%Y%m%d-%H%M%S)" "$SNAP"
git fetch -q personal main

printf '✓ snapshot %s -> personal/main (parent %s)\n' \
  "${SNAP:0:11}" "${PREV:0:11}"
