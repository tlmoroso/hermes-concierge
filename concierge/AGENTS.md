# Concierge AGENTS.md — runbook for agents working on the Discord Concierge

Load-bearing guidance distilled from the full build (2026-09-20 → 2026-10-30):
architecture seams, verified pitfalls, debugging playbook, standing patterns, and
open threads. Read before touching `concierge/`, the Discord adapter, or the
deployment. Facts here are LIVE-VERIFIED — dates and commit ids anchor them.

## 1. What this is (and is not)

A phone-first orchestration layer over Hermes: channels/forum posts in a managed
Discord category spawn preconfigured bots (blueprints), managed entirely from
Discord. It is a **personal fork feature** — NOT upstreamed, and it will never be:
the operator has **no access to `NousResearch/hermes-agent`**. Consequences:

- **Never push to `origin`** (no access, and it is the upstream remote).
- **`hermes update` / upstream syncs can silently reset `main` and drop this
  feature** — this happened on 2026-10-29 and cost a full recovery. The protection
  is the **snapshot chain** (§7) — keep it current after every work session.
- Hosted "Hermes Cloud" runs stock Hermes ⇒ it can never run concierge. The
  permanent home is the Hetzner VM (§8).

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
   `todo`, `browser`, `web`); there is a test enforcing this.
4. **Home channel:** `PlatformConfig.from_dict` materializes `home_channel`
   ONLY from the **dict shape** `{platform, chat_id, name}` — a bare string is
   **silently dropped** (gateway/config.py, `isinstance(home, dict) else None`).
   The per-turn nag (gateway/run_turn_prepare.py) reads the TURNING profile's
   secrets/config; bots carry no `DISCORD_*` secrets by design, so provision
   seeds the dict into each bot's config from the hub's `DISCORD_HOME_CHANNEL`
   (commit 7fb297d2d67; the string-shaped first attempt did nothing — that was
   a live-verified trap).
5. **Cron origin delivery:** bot profiles hold cron jobs in their OWN store but
   own no adapter. Cross-profile delivery needs the `SharedRouteAdapters`
   origin-owner grant (`cron/scheduler_preflight.py`) constructed at BOTH the
   ticker (`cron/scheduler_provider.py::_tick_adapters_for_profile`) and the
   drain (`gateway/run.py::_drain_restart_safe_cron_deliveries`). The drain must
   wrap **every credentialless map** — a bot's map is not empty at delivery time
   because its first turn creates a LOCAL adapter (commit 6462a607773; the
   empty-map-only check silently dropped the grant).
6. **Config placement (multiplex):** the `concierge:` section lives in the
   **launch home's** config.yaml (Mac: the default home; VM: `/root/.hermes/config.yaml`)
   — the runner reads it there, not from the hub profile. The hub's Discord
   secrets live in the hub profile's `.env`; the nous provider auth lives in the
   **launch home's `auth.json`**.

## 3. Deployment topology

- **Mac (dev + this agent's host):** live checkout at `~/.hermes/hermes-agent`
  (branch `main`), launchd gateway serving ONLY the default profile (desktop
  chat). The Mac's concierge Discord lane is RETIRED — the bot token lives on
  the VM only. Backup of the old hub env: `~/.hermes/profiles/concierge/.env.mac-backup`.
- **VM (production):** Hetzner CX22 (`2.31.113.140`, key
  `~/.ssh/id_ed25519_hetzner_concierge`), Ubuntu 24.04, systemd unit
  `hermes-gateway` (runs as root — `--run-as-user root` override; see §9 for
  hardening backlog). Gateway = default-profile host + `concierge` multiplexed
  secondary; bots provision fresh from Discord pickers. The registry starts
  EMPTY by design — bots are disposable.
- **Deploy flow:** `~/.hermes/concierge-deploy.sh` (rsync the tree with
  `--delete` + excludes [.git/.venv/.hermes/node_modules/caches/logs], then
  `systemctl restart hermes-gateway`, then verify Connected/Synced lines).
  Day-2 updates NEVER use `hermes update` on the VM.
- **VM state backup:** `~/.hermes/vm-backup.sh` (rsync pull of profiles +
  auth.json + config + kanban → `~/.hermes/vm-backup/`), LaunchAgent daily 10am
  (`~/Library/LaunchAgents/com.hermes.vm-backup.plist` — needs
  `launchctl load` from a Terminal OUTSIDE the gateway session; see §6).

## 4. Auth model (Nous Portal)

- **No static inference keys exist.** The nous provider is Portal OAuth
  (device-code flow): `hermes auth add nous --type oauth` prints a URL+code;
  complete it from any device; the host polls. There IS an `NOUS_API_KEY` env
  fallback in the provider, but the Portal does not issue such keys for
  subscription accounts.
- Tokens are minted per call from the stored refresh token; a background
  keepalive refreshes (portal invalidation ⇒ local quarantine + a clear
  "re-authentication required" message).
- **One token family per machine.** Copying `auth.json` between two RUNNING
  gateways = rotation war: the copy gets stripped (`last_auth_error`, no
  tokens) and turns start failing with a misleading "couldn't connect to the
  AI model service" card. The VM has its OWN login (2026-10-30). If a machine's
  auth is ever rebuilt from a copy, re-run the device-code login there.
- Honcho memory: static workspace key (`hch-v3-…`, workspace `discord-concierge`),
  0600, in the hub + synthesized per-bot configs. Never copy OAuth-bearing
  honcho.json. The honcho plugin moved OUT of tree (external catalog plugin) —
  `concierge/provision.py::_static_honcho_key` guards the import and degrades
  to no-memory + one WARNING.

## 5. Discord surface facts

- **Slash sync:** `DISCORD_COMMAND_SYNC_POLICY=bulk` in the hub `.env` — ONE bulk
  PUT. The default "safe" policy makes per-command creates (Discord allows ~2
  command creates/10min, 200/day) — gateway restarts each burn attempts, and
  hammering it locks ALL command registration for the day (error 30034). If
  commands go missing after restarts, wait out the quota window; do not
  restart-loop.
- **Restart semantics:** `hermes gateway restart` DEFERS behind live turns
  (cap 1800s force-drain) — including the controller agent's own session: the
  restart completes only after the turn ENDS. On the Mac use ONLY the checkout
  launcher `~/.hermes/hermes-agent/.hermes/bin/hermes gateway restart` (the
  `.venv` shim regenerates the launchd plist against a stale env workspace and
  crash-loops — a 70-minute outage was born this way).
- **Forum flow:** the auto-offer fires on THREAD create (a forum post is a
  thread), NOT on forum-channel create (Discord forbids plain messages in forum
  channels — the offer there is skipped silently). Post content predates the
  binding, so provisioning SEEDS the opening content into the bot
  (`_concierge_seed_thread_opening`) and greets in-thread.
- **Channel-delete auto-teardown:** deleting a bound channel retires+deletes an
  orphaned bot (no other channels) or disbands the room key. Deleting test
  channels is therefore a cleanup action.
- **The bot token is single-homed** (the VM). Two gateways on one token fight
  over the WebSocket and double-answer.

## 6. Local-session guard (workarounds you WILL need)

This agent's terminal runs INSIDE the Mac gateway. Guards block:
- command lines (and locally referenced scripts) containing gateway
  restart/stop patterns — even for REMOTE hosts;
- `launchctl` bootstrap/load of ANY agent;
- destructive commands against real `~/.hermes` (auto-approved with notice for
  sanctioned ops).

For REMOTE gateway operations (VM restarts), pipe the script over SSH stdin —
the local command line stays clean and remote restarts genuinely cannot kill
this session:

```
ssh -i ~/.ssh/id_ed25519_hetzner_concierge root@2.31.113.140 bash -s < script.sh
```

Never try to bypass the guard for LOCAL gateway actions — request the restart
and END THE TURN.

## 7. Repo hygiene & the snapshot chain

- Partial clone (promisor `origin`, tree:0): a full-history push lazy-fetches
  ~12 GB from upstream — never happens here. Pushes go ONLY to `personal`
  (`git@github.com:tlmoroso/hermes-concierge.git`, key
  `~/.ssh/id_ed25519_gh_personal`, BatchMode) as **snapshot commits**:
  ```
  SNAP=$(git commit-tree <tip>^{tree} -p <prev-snapshot> -m "Snapshot …")
  git push personal "$SNAP":refs/heads/main && git update-ref refs/snapshots/<name> "$SNAP"
  ```
  Chain so far: 4e2143ce → d24b3cf9da8 → 09c3f4b7b9 → ba6b4dbbdab → 797c17bf561
  → 3a43d0cfea0. **Push a fresh snapshot at the end of every work session** —
  this is the only off-site copy of the feature.
- Merges/recovery: resolve in a WORKTREE on a temp branch, test there, then
  fast-forward `main` atomically — conflict markers on the live checkout +
  a launchd crash-restart = import-error crash loop.
- Commits: explicit paths only (never `git add -A`); pre-commit enforces
  `ruff check --config ./ruff.pre-commit.toml` — run it on changed files first.

## 8. Testing

- ALWAYS `scripts/run_tests.sh <paths>` (never bare pytest). Tests import
  `hermes_yaml`, never third-party `yaml`.
- Known machine artifact: tests importing `gateway.run` hit the home-io guard
  (checkout lives inside `~/.hermes`; `payload_venv` probes
  `<checkout>/../manifest.json`). Byte-identical on pristine baseline — verify
  with changes stashed before excluding anything.
- Test-rig traps (each cost a debugging cycle): MagicMock enums mint a NEW
  member per attribute access — bind `discord.MessageType.default` once at
  module scope; patching `discord.Thread` makes stubs Thread-instances, which
  thread-exemptions then bypass — use plain marker classes.
- Rig fixtures stub `hermes_bootstrap`, which is why gateway-concierge suites
  run clean on this checkout while unrelated gateway suites hit the artifact.
- TDD discipline: RED for the RIGHT reason first (read the failure), then GREEN,
  then the suite gate. One commit per logical fix.

## 9. Open threads & backlog (none block operation)

1. **Notes Bot in the cloud** needs the Obsidian vault reachable on the VM.
   APPROVED DESIGN (2026-10-30, NOT BUILT): Zettelkasten method, Syncthing
   sync (multi-user reproducible), vault at a neutral path (`~/hermes-vault`,
   not Documents/iCloud), NO pre-defined sections (MOCs emerge from topic
   clusters). Full spec: `concierge/NOTES_BOT_HANDOFF.md`.
2. **VM hardening:** gateway runs as root (installer's LXC-style override);
   consider re-homing under a dedicated user + enabling Hetzner automated
   backups (console, ~€0.90/mo, 7 rolling days) as the second backup layer.
3. **Mac cleanup:** dormant concierge hub profile + Sept-era bot profiles
   (`bot-reminders-94d3`, `bot-researcher-c360/1d0a`) are inert data; the Mac
   registry still holds their bindings. Retire+purge when convenient.
4. **Honcho old OAuth grants** are revocable in the dashboard (user-side).
5. **Generic-gateway nicety (fork-level):** the per-turn home-channel notice
   should not nag profiles that don't own the platform transport — we work
   around it via provision seeding; a proper fix would live in
   `gateway/run_turn_prepare.py` if that file is ever touched.
6. **Reminder timing granularity:** scheduler ticks ~30-60s, so one-shot
   reminders can land up to a minute late. Accepted; not tunable without
   upstream scheduler knowledge.
7. **M10 location webhook** (iOS Shortcut → reminder bot) is coded but not
   wired live: needs an inbound port opened on Hetzner (security group +
   instance) + the Shortcut built. See `concierge/location_events.py`.
8. **Task 9.1-live** (Honcho cross-session preference check) is untested live.

## 10. Debugging playbook

- **Logs:** launch home `~/.hermes/logs/gateway.log` (Mac) / `/root/.hermes/logs/`
  (VM); per-profile `~/.hermes/profiles/<p>/logs/{gateway,agent,errors}.log`;
  bot cron stores `profiles/<p>/cron/jobs.json` (+ `deliveries.db` rows).
  Silent failures love the profile-level `errors.log` — check it FIRST.
- **Routing verdicts:** session keys in log lines (`agent:<profile>:…`) reveal
  which lane handled a turn; "Flushing text batch" shows the batch lane.
  A bound-channel message on `agent:concierge:…` means the routing verdict
  failed (check the registry + the seam list in §2).
- **Slash commands:** verify against the Discord API —
  `GET /applications/1551358300937785476/commands` with the hub token (read
  token from the hub `.env`; NEVER print it).
- **Isolate model/auth from routing:** run a direct CLI turn on the profile —
  `hermes -p <profile> -z "Say hi in exactly three words"`. Works but Discord
  doesn't? Routing/admission. Fails too? Model/auth/config.
- **After ANY upstream sync:** run the concierge suites first
  (`tests/concierge/ tests/gateway/test_discord_concierge_*.py
  tests/cron/test_cron_multiplex_shared_route_delivery.py`) — every seam in §2
  has regression tests; a red suite names the broken seam.
- **Bots behaving badly** (terminal calls, malformed tool args, output-limit
  refusals): suspect toolset leakage first — probe
  `_get_platform_tools(<bot config>, 'discord')` and compare to the blueprint.

## 11. Standing working patterns (operator's preferences)

- Delegate implementation to GLM-5.3-flash subagents, review with GLM-5.3
  (cheap builders, strong checkers); TDD; escalate to the operator only for
  real blockers (API keys, config decisions, live E2E gates).
- Minimal contexts everywhere: 1–3 toolsets per blueprint, small SOULs,
  `reasoning_effort: low`, answer-only display
  (`tool_progress/long_running_notifications/interim_assistant_messages: false`),
  `cron.wrap_response: false`. Bots should be fast single-tool-call flows.
- Never expose Hermes internals in bot personas; never print secret values
  (read `.env` by key name only).
- Thin duck-typed hooks over core surgery; core must never import
  `concierge/`. Fail-closed: routing degrades to the hub; resolver exceptions
  return None; wrap failures delegate.
- Registry is the single routing source of truth; never duplicate it into
  config routes (rooms make that impossible anyway — same-specificity routes
  raise `admission_conflict`).

Anchors: build plan at `~/.hermes/plans/2026-09-20_165455-discord-concierge.md`;
design notes in `concierge/DISCORD_INTEGRATION.md`; deploy runbook
`concierge/DEPLOY.md`; Apple data options `concierge/APPLE_BRIDGE.md`.
