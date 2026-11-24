#!/usr/bin/env bash
# Concierge deploy — ship the checkout to the Hetzner VM.
#
# HARD POLICY (operator, 2026-11-14): ONLY main is ever deployed.
#   feature branch -> rebase onto main -> fast-forward merge -> test -> deploy main.
# There is deliberately NO force/override flag. The rule is not optional, and an
# escape hatch is how it gets skipped at 2am.
#
# Usage:
#   scripts/concierge/deploy.sh            # preflight + rsync + restart + verify
#   scripts/concierge/deploy.sh --no-restart   # rsync only
#   scripts/concierge/deploy.sh --verify       # inspect VM state, deploy nothing
#
# Restarts are blocked when run from inside the gateway process (Hermes' own guard:
# SIGTERM would propagate to the child). Run it from a plain shell.
set -euo pipefail

REPO="${CONCIERGE_REPO:-$HOME/.hermes/hermes-agent}"
VM_IP="${CONCIERGE_VM_IP:-2.31.113.140}"
VM="root@${VM_IP}"
KEY="${CONCIERGE_VM_KEY:-$HOME/.ssh/id_ed25519_hetzner_concierge}"
SSHCMD="ssh -i $KEY -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new"
DST="$VM:/root/.hermes/hermes-agent/"

EXCLUDES=(
  --exclude '.git/' --exclude '.venv/' --exclude 'venv/' --exclude '.hermes/' --exclude 'node_modules/'
  --exclude '__pycache__/' --exclude '*.pyc' --exclude '.pytest_cache/'
  --exclude '.ruff_cache/' --exclude 'logs/' --exclude '.DS_Store'
  --exclude '.pytest-cache/' --exclude 'MagicMock/'   # local test junk, never shipped
)

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

preflight() {
  cd "$REPO"
  git rev-parse --git-dir >/dev/null 2>&1 || die "not a git repo: $REPO"

  local branch
  branch="$(git rev-parse --abbrev-ref HEAD)"
  [ "$branch" = "main" ] || die "refusing to deploy from '$branch' — only main is deployed.
  Rebase the branch onto main, fast-forward merge it, then deploy."

  [ "$(git symbolic-ref -q --short HEAD || echo detached)" = "main" ] \
    || die "HEAD is detached — check out main before deploying."

  [ -z "$(git status --porcelain)" ] || die "working tree is dirty:
$(git status --porcelain | head -20)
  Commit (or stash) first — a half-applied tree on the VM is an import-error crash loop."

  [ "$(git rev-parse HEAD)" = "$(git rev-parse main)" ] \
    || die "HEAD is not at the main tip — refusing a partial deploy."
}

case "${1:-deploy}" in
  --verify)
    $SSHCMD $VM 'systemctl is-active hermes-gateway; tail -3 /root/.hermes/profiles/concierge/logs/gateway.log 2>/dev/null' ;;
  --no-restart|deploy)
    preflight
    # Deliberately NOT the naive ``-a --delete``: ``-a`` chowns the whole VM
    # tree to the LOCAL uid (10000) and ``--delete`` removes VM runtime markers
    # (install-stamp.json, .bytecode-fingerprint) that are not in the checkout.
    # --checksum avoids re-transferring ~22k files whose mtimes merely differ;
    # full-tree hash verification after the sync proves nothing was missed.
    rsync -az --checksum --no-owner --no-group "${EXCLUDES[@]}" -e "$SSHCMD" "$REPO/" "$DST"
    printf '✓ synced main (%s) to %s\n' "$(git -C "$REPO" rev-parse --short HEAD)" "$VM_IP"
    # SOUL.md is written ONCE, at provisioning; create_bot's idempotence paths
    # return existing bots AS-IS. A catalog/SOUL edit in the synced code would
    # otherwise NEVER reach an already-provisioned bot (2026-11-24: the live
    # orchestrator kept obeying the old persona for days). Re-render every
    # registered bot's persona from the CURRENT catalog BEFORE the restart.
    # Uses the checkout launcher (the .venv shim is a Mac binary on this VM).
    echo "→ refreshing bot SOULs from the current catalog"
    $SSHCMD $VM '/root/.hermes/hermes-agent/.hermes/bin/hermes -p concierge concierge refresh-souls'
    if [ "${1:-deploy}" = "--no-restart" ]; then echo "✓ no restart (--no-restart)"; exit 0; fi
    echo "→ restarting gateway"
    $SSHCMD $VM 'systemctl restart hermes-gateway'
    sleep 70
    $SSHCMD $VM 'systemctl is-active hermes-gateway && tail -3 /root/.hermes/profiles/concierge/logs/gateway.log'
    echo "✓ deployed — check the Connected/Synced lines above" ;;
  *) die "unknown option: $1 (use --verify or --no-restart)" ;;
esac
