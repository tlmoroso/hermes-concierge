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
- **Dispatch:** a SOUL-driven directive, NOT a tool — the orchestrator's entire
  reply is `@dispatch <blueprint-key> <task>`; the `set_message_handler` wrap
  parses it (registry-confirmed orchestrator turn, human-authored, no
  continuation stamp) and returns None (the directive is never posted/spoken).
  A background driver posts the visible `→ asked <Title>: <task>` handoff,
  lazily provisions the per-user channel-less worker + its channel, runs the
  worker's turn through the member-turn machinery, parses the reply MESSAGE-wise
  (multi-line answers reach phase B whole), and runs a continuation-stamped
  orchestrator turn that summarizes into the home channel — the ONE spoken
  playback on voice. Fail-closed ladder at every step; depth 1; bot-authored
  never dispatches.
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
