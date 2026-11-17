# Handoff — feat/notes-bot-zettelkasten (historical)

> **Status: LANDED ON MAIN — branch superseded, do not build from it.**
> Every item listed below now exists on `main` (verified 2026-11-17). This
> document is kept for the record and for the open items in the next section,
> which are still outstanding. Read `concierge/AGENTS.md` first.

Branch: `feat/notes-bot-zettelkasten` (on `origin`). Base: `main` @ 30a475aa.

Despite its name, this branch contains **no Zettelkasten / notes-bot work** —
it is the multi-user (admission, tiers, session isolation) branch. The notes
bot is a separate, still-unbuilt design: see
`concierge/handoffs/NOTES_BOT_HANDOFF.md`.

## What's implemented

- **Phase 1 — Admission + tiers:** `DISCORD_ADMIN_USERS` / `DISCORD_USER_USERS` env vars (config_defaults.py) + `concierge/auth_tiers.py`. `/bot new`, `/room create` open to any admitted user; `/bot retire`, `/room add/remove/disband`, `/connect`, `/bot category` admin-gated.
- **Phase 2 — Session isolation:** invariant tests in `tests/gateway/test_session_user_isolation.py` (distinct users → distinct session keys).
- **Phase 3 — Memory:** fallback documented (multi-user bots disable memory).
- **Phase 4/5 — Developer blueprint:** `developer` blueprint (file + terminal + web), admin-only.
- **Personal categories:** `/bot category <user>` creates a permission-locked Discord category.
- **Plan doc:** `concierge/handoffs/MULTI_USER_PLAN.md` updated to personal-category model.

## Where each item lives on main today

| Branch file | On main |
| --- | --- |
| `concierge/auth_tiers.py` | present, **newer** — reads env through `platform_gate_env` so allowlists resolve under multiplexing; the branch's `os.getenv` version is wrong in that mode |
| `concierge/blueprints.py` | `developer` blueprint present |
| `hermes_cli/config_defaults.py` | `DISCORD_ADMIN_USERS` / `DISCORD_USER_USERS` present |
| `plugins/platforms/discord/concierge_mixin.py` | `/bot category` moved to `concierge_mixin_admin.py`; overwrites built by `concierge_mixin_home_space.py`; `/bot space` added |
| `tests/concierge/test_auth_tiers.py` | byte-identical |
| `tests/gateway/test_session_user_isolation.py` | byte-identical |
| `concierge/MULTI_USER_PLAN.md` | moved to `concierge/handoffs/MULTI_USER_PLAN.md`, byte-identical |

The branch is 9 commits ahead / 20 behind `main` and conflicts on
`auth_tiers.py`, `blueprints.py` and `concierge_mixin.py`. Do not merge it;
`main` is the authority.

## Open items (still not implemented)

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

- Single-owner deployments remain backward compatible (admin defaults to allowed users).
