# Orchestrator front door

Spec: `concierge/ORCHESTRATOR_DESIGN.md`. Commits `f095e5e1432..fa0f8b5c371`
(subagent-built, two-stage reviewed). Gate: 27 files / 606 tests green.

- **Model:** one orchestrator bot + one personal channel per Discord user. The
  registry `users` section (`{user_id: {orchestrator, home_channel, guild_id,
  workers}}`) is the ownership record; `routing.py` is UNTOUCHED — personal
  channels are ordinary bindings, and admission confirms orchestrator stamps via
  a third branch (c) in `_concierge_dynamic_runtime_profile` (bindings/rooms
  still win).
- **Bootstrap:** first human message in an unbound MANAGED-category channel with
  no live orchestrator spawns a single-flight background bootstrap (provision →
  personal channel → bind → greeting). Three-posture guard: ghost/stat_error
  entries NEVER bootstrap (a dead entry must not wedge a bound channel). Hot
  path keeps ONE registry read per message.
- **Dispatch is a REAL TOOL (2026-11-24):** `concierge_dispatch` is a
  schema-backed tool (`tools/concierge_dispatch_tool.py`, toolset
  `concierge_dispatch`); the orchestrator's toolsets are now
  `("web", "concierge_dispatch")`. The model CALLS it with a `blueprint`
  (enum of worker keys) and a `task`. The tool validates the blueprint and the
  shared guards, records the intent in a process-local slot keyed by the live
  event, and the wrap consumes it after the turn — suppressing the model's
  final text and spawning the driver. The magic `@dispatch <key> <task>` text
  directive remains in the wrap ONLY as a legacy fallback for profiles whose
  SOUL.md has not been refreshed; new SOULs never mention it. Guards
  (`_concierge_dispatch_eligible`) are shared by both paths: human-authored,
  no continuation stamp, registry-confirmed orchestrator turn.
- **Legacy text directive (fallback):** the OLD protocol had the orchestrator's
  entire reply be `@dispatch <blueprint-key> <task>`; the `set_message_handler`
  wrap parsed it (registry-confirmed orchestrator turn, human-authored, no
  continuation stamp) and returned None. It is retained ONLY for profiles
  whose SOUL.md has not been refreshed. The driver is the same for both paths:
  A background driver SILENTLY (no `→ asked <Title>: <task>` intermediation —
  removed 2026-11-24; the full task goes to the worker as inbound turn text,
  never posted to Discord) lazily provisions the per-user channel-less worker
  + its channel, runs the worker's turn through the member-turn machinery,
  parses the reply MESSAGE-wise (multi-line answers reach phase B whole), and
  runs a continuation-stamped orchestrator turn that summarizes into the home
  channel — the ONLY visible response, and the ONE spoken playback on voice.
  Terminal failure notices (`⚠ dispatch failed`, `⚠ <bot> is unavailable`)
  go through one visible-post formatter (`_concierge_post_visible`, backed by
  the adapter's own `MAX_MESSAGE_LENGTH` + `truncate_message` chunker) so no
  notice can repeat the 50035 / 2000-char abort. Fail-closed ladder at every
  step; depth 1; bot-authored never dispatches.
- **Files / artifacts (2026-11-24):** the orchestrator stays file-less (three
  tools: `web_search`, `web_extract`, `concierge_dispatch`) and CANNOT write
  files. A `file`-toolset
  `artifacts` worker (Files Bot) writes the document into the per-user
  workspace and answers `ARTIFACT:<absolute path>`. The driver validates that
  path against the workspace root (untrusted worker text — no arbitrary host
  upload), stamps it on the phase-B event, and the wrap appends `MEDIA:<path>`
  deterministically so the user gets a downloadable attachment even if the
  model forgot the tag. The SOUL also asks for the tag: belt and braces.
- **SOUL guardrails (2026-11-24):** the orchestrator SOUL now states its exact
  three tools, that `concierge_dispatch` is the ONLY dispatch mechanism, that
  it must not do research itself, that it gets NO progress information (never
  claim a worker is "still running"), and how to handle files. `tests/concierge/
  test_blueprints.py` locks those lines verbatim. Two code-level guardrails
  back them: `_concierge_guard_visible_response` scrubs malformed multi-line
  `@dispatch` leaks and delivers artifacts; `_concierge_safe_artifact_path`
  gates the upload path.
- **Vault (notes bot):** `notes` is the only `vault=True` blueprint.
  Provisioning creates a DURABLE per-user Obsidian vault OUTSIDE the disposable
  bot profile at `~/.hermes/vaults/<user_id>/`, seeds `CONVENTIONS.md` (the
  rulebook the SOUL references) + `Home.md` (MOC root) only when missing, and
  pins `OBSIDIAN_VAULT_PATH` in the bot profile's `.env` (0600) — the path
  convention the obsidian skill resolves. Cross-device sync (git remote /
  Syncthing / iCloud) is an operator choice layered on afterwards; see
  `concierge/APPLE_BRIDGE.md`.
- **SOUL staleness — DEPLOY REQUIREMENT:** `SOUL.md` AND the three toolset keys
  in config.yaml are written ONCE at provisioning, and `create_bot`'s
  idempotence paths never rewrite them, so a catalog change synced to the VM
  does NOT reach an existing bot.
  `hermes -p concierge concierge refresh-souls` re-renders every registered
  bot's persona, re-pins toolsets from the current blueprint, and ensures the
  vault (persona names preserved, orphans skipped, idempotent).
  `scripts/concierge/deploy.sh` runs it after the rsync and BEFORE the restart.
  Always run it after any catalog/SOUL/toolset change.
- **Tool handlers run in a DIFFERENT HERMES_HOME context (2026-11-25):** the
  gateway multiplexes profiles; `gateway/run.py::_profile_runtime_scope` sets a
  `HERMES_HOME` ContextVar override to the per-bot profile inside the agent
  worker. `concierge.registry.registry_path()` resolves `get_hermes_home()`, so
  a registry lookup done INSIDE a tool handler reads
  `<bot profile>/concierge/registry.json`, which does not exist — an empty
  registry, `orchestrator_for_user()` → None, and every dispatch refused with
  "this is not an eligible orchestrator turn". The old text-directive guard ran
  in the wrap (hub context) and never hit this. Fix: the wrap captures the hub
  home (I/O-free `get_hermes_home()`) and the tool handler re-pins it around the
  shared `_concierge_dispatch_eligible` guard. Test-locked by
  `test_dispatch_tool_survives_worker_hermes_home_override`. Any NEW tool that
  reads concierge state must do the same — never assume the handler sees the
  hub profile.
- **New toolsets must be EAGER:** on the VM `snowballstemmer` is installed, so
  `tools.tool_search` is ACTIVE and a non-core toolset gets deferred behind the
  `tool_search`/`tool_call` bridge. `concierge_dispatch` is registered with
  `platforms=frozenset({"discord"})` in `toolsets.py`, which keeps it direct and
  out of default composites. Verify on the VM runtime (the local scratch venv
  lacks snowballstemmer, so local smoke tests silently pass).
- **Voice:** the restamp runs the registry verdict FIRST (binding/room channels
  keep their routing), then re-homes unbound channels to the speaker's
  orchestrator via the `_CONCIERGE_ORCHESTRATOR_ATTR` stamp — no category check
  (spoken input is user-scoped). No relay changes.
- **Provisioning:** `create_bot(user_id=…, channel_id=None)` = user-keyed
  idempotent (per-user lock); orchestrator reprovision/adopt swaps preserve
  home_channel/guild_id/workers (only the orchestrator field changes —
  `clear_orchestrator`). Multi-user cron skip no longer applies to per-user
  bots; routine origin = bound channel → home_channel → deferred.
- **Picker excludes the orchestrator** (auto-provisioned; CLI `new orchestrator
  --user <id>` only). `--channel` XOR `--user` enforced in the CLI handler.
- **Suites:** tests/concierge/test_registry_users.py, test_provision_users.py;
  tests/gateway/test_discord_concierge_orchestrator_{fallback,dispatch}.py,
  test_discord_concierge_voice_orchestrator.py; CLI rows in
  tests/hermes_cli/test_concierge_cli.py.

Anchors: build plan at `~/.hermes/plans/2026-09-20_165455-discord-concierge.md`;
design notes in `concierge/DISCORD_INTEGRATION.md`; deploy runbook
`concierge/DEPLOY.md`; Apple data options `concierge/APPLE_BRIDGE.md`.
