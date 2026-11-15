# Repo hygiene & the snapshot chain

- Partial clone (promisor `origin`, tree:0): a full-history push lazy-fetches
  ~12 GB from upstream — never happens here. Pushes go ONLY to `personal`
  (`git@github.com:tlmoroso/hermes-concierge.git`, key
  `~/.ssh/id_ed25519_gh_personal`, BatchMode) as **snapshot commits**:
  **Do this with** `scripts/concierge/snapshot.sh "message"` — it performs exactly the
  above and refuses anything but a clean `main`. Do not hand-roll it.
  ```
  SNAP=$(git commit-tree <tip>^{tree} -p <prev-snapshot> -m "Snapshot …")
  git push personal "$SNAP":refs/heads/main && git update-ref refs/snapshots/<name> "$SNAP"
  ```
  Chain so far: 4e2143ce → d24b3cf9da8 → 09c3f4b7b9 → ba6b4dbbdab → 797c17bf561
  → 3a43d0cfea0 → 04161a5d38b (voice) → e7fa0152c3b (hy4-presets) → 8c3f92ee833
  (orchestrator) → 96ed3d84575 (orchestrator-hotfix1) → 81029ebb073 (pm member
  workspace fix: honcho catalog install) → 4766432bad4 (orchestrator main + voice
  auto-join). **Push a fresh snapshot at the end of every work session** —
  this is the only off-site copy of the feature.
- **Branch + deploy policy (operator, 2026-11-14):** every change is committed on a
  feature branch; the branch is REBASED onto `main`, then fast-forward merged into
  `main`. **Only `main` is ever deployed to the VM** — never a feature branch. The
  merge is resolved in a WORKTREE first (see below), tested there, and only then is
  `main` fast-forwarded, because conflict markers on the live checkout plus a
  launchd crash-restart is an import-error crash loop. Mechanics:
  `scripts/concierge/deploy.sh` (refuses a non-`main` or dirty tree; **no override
  flag**) and `scripts/concierge/snapshot.sh` (the off-site copy). Neither can be
  run from inside the gateway process — Hermes' own guard blocks a restart that
  would SIGTERM the session running it, so use a plain Terminal.
- **Ratchet baseline:** the fork's own `main` only. `origin/main` is upstream
  NousResearch (no concierge code there — measuring against it counts long-merged
  fork work as "new"), and `personal/main` is this snapshot chain, not a history
  ancestor. See `scripts/code_health/gitio.py::default_base`.
- Merges/recovery: resolve in a WORKTREE on a temp branch, test there, then
  fast-forward `main` atomically — conflict markers on the live checkout +
  a launchd crash-restart = import-error crash loop.
- Commits: explicit paths only (never `git add -A`); pre-commit enforces
  `ruff check --config ./ruff.pre-commit.toml` — run it on changed files first.
