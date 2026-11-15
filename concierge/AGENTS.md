# Concierge AGENTS.md — hub for agents working on the Discord Concierge

Load-bearing guidance for this area of the tree. Facts are LIVE-VERIFIED — dates
and commit ids anchor them. Read this first, then open the one doc you need.

**Layout:** this hub carries what applies everywhere in `concierge/` plus a
routing table. Everything long-form lives in `docs/` beside it, one file per
topic. That split is what keeps the root→area chain inside its character budget
(`scripts/ci/check_agents_md_size.py`: root ≤ 12k, each root-to-area chain ≤
30k) — so grow `docs/`, never this file.

## 1. What this is (and is not)

A phone-first orchestration layer over Hermes: channels/forum posts in a managed
Discord category spawn preconfigured bots (blueprints), managed entirely from
Discord. It is a **personal fork feature** — NOT upstreamed, and it will never be:
the operator has **no access to `NousResearch/hermes-agent`**. Consequences:

- **Never push to `origin`** (no access, and it is the upstream remote).
- **`hermes update` / upstream syncs can silently reset `main` and drop this
  feature** — this happened on 2026-10-29 and cost a full recovery. The protection
  is the **snapshot chain** — keep it current after every work session.
- Hosted "Hermes Cloud" runs stock Hermes ⇒ it can never run concierge. The
  permanent home is the Hetzner VM.

## 2. The seams (where concierge plugs into the gateway)

The upstream refactor of 2026-10 moved the live inbound chain. Verify against
these before assuming any hook is live:

1. **Inbound message chain:** `on_message` → text-batch flush →
   `BaseAdapter._start_session_processing` (gateway/platforms/base.py:~3833) →
   `_process_message_background` (:~4465) → `self._message_handler(event)` —
   the **RUNNER's** handler, installed via `set_message_handler` (base.py:~2263).
   The adapter's `handle_message` is **NOT in the live chain** — an override
   there is dead code (this orphaned the room fan-out once; commit 91c63037594
   moved it onto the `set_message_handler` seam). The concierge wrap:
   `_concierge_inbound_dispatch` in `plugins/platforms/discord/concierge_mixin.py`
   (room-targets stamp → fan-out driver, never delegate; connect-nudge → post +
   delegate; else 1:1 delegate; fail-closed on every branch).
2. **Admission (the `profile_mismatch` guard):** `gateway/session_ingress_context.py`
   `_binding`/`_runtime_profile` resolves the runtime profile from
   `config.profile_routes` ONLY. Registry-stamped sources (bot profiles) need
   `runner._dynamic_runtime_profile` — a duck-typed resolver the mixin installs
   (commit ae9cfcde211). It returns `source.profile` ONLY when the registry
   confirms it (binding or room membership); homes-membership and
   `authority_for_home` checks still run — never bypass them.
3. **Toolset resolution:** the Discord lane reads `platform_toolsets.discord`
   (`hermes_cli/tools_config.py::_get_platform_tools`); the **top-level
   `toolsets` key is the CLI/local lane only**. The cron lane prefers the job's
   own `enabled_toolsets`, else `_get_platform_tools(cfg, 'cron')`. Provision
   seeds `platform_toolsets.discord` AND `.cron` (commits 535172effad,
   6462a607773). Composite names (`hermes-cli`) re-expand to the full ~17-set
   range — blueprints must pin real bundles (`file`, `terminal`, `cronjob`,
   `todo`, `browser`, `web`) there is a test enforcing this.
4. **Home channel:** `PlatformConfig.from_dict` materializes `home_channel`
   ONLY from the **dict shape** `{platform, chat_id, name}` — a bare string is
   **silently dropped** (gateway/config.py, `isinstance(home, dict) else None`).

## 3. Long-form docs (open the one you need)

| Doc | Read when you are working on |
|---|---|
| `docs/seams.md` | the full ingress chain, admission, toolsets, home channel, cron delivery |
| `docs/deployment-topology.md` | Mac/VM layout, services, deploy flow, backups |
| `docs/auth-model.md` | Nous Portal OAuth, token families, Honcho memory |
| `docs/discord-surface-facts.md` | slash sync, forum flow, channel-delete teardown |
| `docs/local-session-guard.md` | the terminal guard and its workarounds |
| `docs/testing.md` | how to run the suites |
| `docs/backlog.md` | open threads |
| `docs/debugging.md` | routing/admission failure triage |
| `docs/working-patterns.md` | how the operator wants work done (delegation, minimal contexts) |
| `docs/voice-front-door.md` | voice ingress + relay |
| `docs/orchestrator-front-door.md` | per-user orchestrator, home category, dispatch |

## 4. Hard rules (everywhere in `concierge/`)

- **Fail-closed, always:** routing degrades to the hub; resolver exceptions
  return `None`; wrap failures delegate. Never a dropped message.
- **The registry is the single routing source of truth** — never duplicate it
  into `gateway.profile_routes` (rooms make that impossible anyway;
  same-specificity routes raise `admission_conflict`).
- **Core must never import `concierge/`**, and plugin code must not either.
  Keep every new capability a thin, duck-typed hook.
- **Never restart the gateway while the checkout is dirty.**
- **Hot path: ONE registry read per message.**
- **Code-health ratchet — do not relax it.** The fork keeps `ENFORCEMENT = "blocking"`
  (`scripts/code_health/config.py`); never flip to `advisory`/`off` to make
  `scripts/check` green. The ratchet's baseline is the fork's own `main` — NEVER
  upstream `origin/main` (NousResearch; concierge does not exist there, so every
  fork file would count as new) and never `personal/main` (a snapshot-artifact
  repo, not a history ancestor — see `docs/repo-hygiene.md`) — configured by
  `scripts/code_health/gitio.py::default_base`, overridable via
  `HERMES_CODE_HEALTH_BASE_REFS`. Policy is read from main, never the branch, so
  the baseline repoint must LAND ON FORK MAIN to apply to work built from it. Fix
  branch-local findings; don't revert the repoint or weaken the switch.

## Topical sibling mixins (file-size ratchet)

The Discord concierge command surface is split out of `plugins/platforms/discord/concierge_mixin.py` into `concierge_mixin_admin.py`, `concierge_mixin_provisioning.py`, `concierge_mixin_room_commands.py`, each a mixin mounted as a `DiscordAdapter` base (no re-export shims; whole moved methods keep their cap). Siblings import facade helpers, never the reverse, so there is no module-level cycle.
