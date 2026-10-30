# Handoff — feat/notes-bot-zettelkasten

Branch: `feat/notes-bot-zettelkasten` (pushed to origin). Base: `main` @ 30a475aa.

## What's implemented

- **Phase 1 — Admission + tiers:** `DISCORD_ADMIN_USERS` / `DISCORD_USER_USERS` env vars (config_defaults.py) + `concierge/auth_tiers.py`. `/bot new`, `/room create` open to any admitted user; `/bot retire`, `/room add/remove/disband`, `/connect`, `/bot category` admin-gated.
- **Phase 2 — Session isolation:** invariant tests in `tests/gateway/test_session_user_isolation.py` (distinct users → distinct session keys).
- **Phase 3 — Memory:** fallback documented (multi-user bots disable memory).
- **Phase 4/5 — Developer blueprint:** `developer` blueprint (file + terminal + web), admin-only.
- **Personal categories:** `/bot category <user>` creates a permission-locked Discord category.
- **Plan doc:** `concierge/MULTI_USER_PLAN.md` updated to personal-category model.

## Open items (not implemented)

1. Honcho per-user partitioning (external) — memory disabled for multi-user bots until verified.
2. Per-user `terminal.cwd` / worktree wiring in provisioning.
3. Per-user todo/cron scoping.
4. `concierge.multi_user` config flag.
5. Two-user E2E verification.
6. `/bot category` not yet wired into provisioning (channels still created manually).

## Verification

- Run `tests/concierge/test_auth_tiers.py` and `tests/gateway/test_session_user_isolation.py`.
- Import check: `from concierge.blueprints import get_blueprint; get_blueprint('developer')`.
- Syntax: `python -c "import ast; ast.parse(open('plugins/platforms/discord/concierge_mixin.py').read())"`.

## Notes for reviewer

- AGENTS.md: never push to origin (upstream). This branch was pushed to the user's fork only.
- Single-owner deployments remain backward compatible (admin defaults to allowed users).
"""