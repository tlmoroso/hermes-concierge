# Orchestrator Redesign — the per-user Concierge front door

Status: DESIGN APPROVED (2026-11-13, operator decisions below). Not built.
Decisions (operator, 2026-11-13):

- **Dispatch:** Discord-mediated, reusing the room machinery (no in-process delegation).
- **Workers:** per-user, not shared across the server.
- **Unbound channels in managed categories:** route to the requesting user's orchestrator.

## 1. Model

"Orchestrator owns the user; channels are windows."

- Every user gets ONE orchestrator bot (`bot-orchestrator-<user>`) and ONE personal
  channel bound to it. The orchestrator is the main conversation surface: users state
  intent there, the orchestrator dispatches work to worker bots and intermediates
  the results back.
- Worker bots are per-user, reusable resources. They are provisioned WITHOUT a
  channel; a channel is created lazily on first dispatch. Users may still talk to a
  worker directly in its channel (existing binding routing, unchanged).
- Specialized create-then-retire bots are the normal lifecycle; nothing about
  `retire` changes.
- Voice spoken input routes to the speaker's orchestrator. The orchestrator's final
  turn contains the dispatched-and-summarized results, so the existing relay
  (`mode: summary`) speaks the right answer with no relay changes.

## 2. Registry v2

Add a `users` section; `bindings` and `rooms` are untouched (v1 registries load fine —
same pattern as the rooms addition).

```json
"users": {
  "<discord_user_id>": {
    "orchestrator": "bot-orchestrator-<suffix>",
    "home_channel": "<channel_id>",
    "created": "<iso>"
  }
}
```

- `routing.py` does NOT change. Personal channels are ordinary bindings to the
  orchestrator profile; direct worker channels resolve as today.
- The `users` section is consulted only by: provisioning (idempotent per user),
  voice ingress (speaker → orchestrator), and the unbound-channel fallback.
- Worker bots become channel-less until first dispatch. `channels_for_profile`
  may return [] for a live bot — every consumer must tolerate that (audit: room
  history, retire, `list`/`bindings` display).

## 3. Dispatch (Discord-mediated)

Reuses the tested room paths: `_concierge_run_member_turn` +
`_concierge_wait_for_member_turn` + `_concierge_fetch_room_history`.

1. Orchestrator decides a worker is needed (SOUL triage + blueprint map; same
   catalog as the picker).
2. Ensure the worker exists for this user (provision on demand, per-user) and has a
   channel (create lazily, bind via `registry.bind_channel`).
3. Post the task to the worker channel; the binding routes it; the worker's turn
   runs with its own narrow toolsets.
4. Orchestrator waits for the reply, summarizes, and answers in the personal
   channel with a visible one-line intermediation ("→ asked Researcher: …").
- Anti-loop and fail-closed guarantees are inherited from the room machinery
  (bot-authored never dispatches; dispatch failure degrades to the orchestrator
  answering alone, never silently dropping).
- The orchestrator's toolset stays minimal (no terminal/file) — dispatch is a
  mixin-provided capability on the wrap, not a generic tool.

## 4. Unbound-channel fallback

In `managed_category_ids`, an unbound channel message routes to the AUTHOR's
orchestrator (provisioning it on first contact if needed) instead of the shared hub
persona. Outside managed categories, behavior is unchanged (hub fallback). The
fallback resolution lives beside `_concierge_dynamic_runtime_profile`; registry still
the single source of truth — the users section, not new config routes.

## 5. Voice

`_restate_voice_routing_stamps` gains a user-resolution branch: spoken input from a
user with an orchestrator stamps the orchestrator profile (personal-lane semantics)
instead of fan-out. No orchestrator → current behavior unchanged (hub lane).
Voice relay config unchanged.

## 6. Per-user cron

Blueprint routines now attach to the user's orchestrator and deliver to their
`home_channel` — removing the multi-user skip in `provision.py` (`_skip cron in
multi-user` goes away for orchestrator-owned routines). Worker bots keep cron
delivery to their own channels (per-user, since workers are per-user).

## 7. Provisioning / CLI

- `hermes concierge new orchestrator --user <discord_id>` (idempotent per user;
  creates the personal channel on first contact when the platform context has one).
- `hermes concierge new <blueprint> --user <id>` provisions a per-user worker
  WITHOUT a channel (`--channel` still supported for direct binds — the two modes
  are: channel-bound v1 style, and user-owned channel-less).
- `list`/`bindings` gain user grouping; `bindings` still the flat channel table.

## 8. Blast radius

| Area | Change |
| --- | --- |
| `concierge/registry.py` | users section, channel-less bots, migrations/cleaning |
| `concierge/blueprints.py` | `orchestrator` blueprint (triage SOUL, minimal toolsets) |
| `concierge/provision.py` | user-keyed provisioning, lazy channel, per-user cron attach |
| `plugins/platforms/discord/concierge_mixin.py` | dispatch capability on the wrap, unbound→orchestrator fallback, home-channel bootstrap |
| `plugins/platforms/discord/concierge_voice.py` | speaker→orchestrator stamp branch |
| `hermes_cli/concierge.py` + `subcommands/concierge.py` | `--user` flag, listing changes |
| tests | registry users, dispatch E2E (mixin suite), voice branch, CLI |

## 9. Invariants to preserve (from the runbook)

- Registry is the single routing source of truth; never duplicate into config routes.
- Fail-closed on every dispatch branch; routing degrades to the orchestrator, never drops.
- Thin duck-typed hooks; core never imports `concierge/`.
- Per-user isolation: session keys include user id (already), Honcho partitions by
  inbound user id (already), terminal cwd namespaced per user (already).
- Hub `.env` allowlist still governs admission for every bot's channels.
