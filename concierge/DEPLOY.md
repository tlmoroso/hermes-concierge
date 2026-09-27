# Concierge cloud deployment runbook (M11, Tasks 11.1 + 11.2)

Deploys the Discord Concierge to an always-on Linux VM: **one** multiplexed Hermes
gateway owns the Discord connection for the `concierge` hub profile and serves every
`bot-*` profile the hub provisions. After deployment the laptop can stay off —
management happens from the Discord mobile app (Task 11.2 acceptance is phone-only).

Status: this is the runbook, verified command-by-command against this checkout
(`hermes_cli/`, `gateway/`, `concierge/`, `website/docs/` — file:line cited where a fact
is load-bearing). It has **not** been executed against a live VM yet; no cloud host is
provisioned. Two commands differ from the plan's text and are marked **Correction** where
they appear (summary in §10).

## 0. Prerequisites — decide before deploying

Decisions recorded 2026-09-21 (previously open questions from the plan):

1. **Hermes Cloud vs self-managed VM (plan, Open question Q1) — DECIDED: hosted Hermes
   Cloud.** Production target is a Hermes Cloud agent (`portal.nousresearch.com/cloud` —
   hosted Hermes Agent instances, managed from the Nous Portal `/agents` page or the
   Portal MCP server; see `website/docs/guides/manage-hermes-cloud-with-mcp.md`).
   Consequence: a hosted instance runs a released image, not a local feature branch, so
   the `concierge` branch must be merged to `main` and carried by a cloud image before
   the hosted path can run (milestone M12 territory). **Until then this VM runbook is the
   executable path** and the reference deployment for validating the feature. The hosted
   management surface today is lifecycle only (create/start/stop/restart/destroy,
   `update_env`/`update_image` via Portal or MCP) — deep profile/config setup on a hosted
   instance must be verified once the feature ships there.
2. **Email in the cloud (Q3) — DECIDED: allowed.** Mail credentials may live on the
   cloud host, so the Email Triage bot is cloud-deployable (himalaya CLI + config + mail
   app password land in the bot profile's `.env`, seeded by provisioning). Note this
   puts mail credentials on hosted infrastructure rather than a VM you control —
   accepted with this decision.
3. **Notes Bot vault reachability (plan, Risks) — still open, decide at deploy.** "The
   Obsidian vault lives on the Mac
   (or iCloud); the M11 Linux VM must reach it (git/syncthing sync of the vault, or
   another mount) or the Notes Bot can't run in the cloud. If sync isn't set up at deploy
   time, the Notes Bot stays a local (Mac) bot — decide at deploy, don't block the launch
   on it." Options are written up in `concierge/APPLE_BRIDGE.md`.

Also required before starting:

- **Workstation, one-time (USER action — deliberately NOT done in this repo):** push the
  branch so the VM can clone it; a local-only branch is invisible to the VM.
  ```bash
  git push -u origin concierge
  ```
- **Discord application** (same setup as the local Task 5.6 E2E): "Server Members Intent"
  + "Message Content Intent" enabled, the bot invited to the server with the
  `applications.commands` scope and **Manage Channels** permission (the hub creates bot
  channels — Task 5.7). Bot token in hand for §3.
- **A Linux VM** (any small cloud box) you can SSH into.

## 1. Host setup (on the VM)

Install Hermes with the official installer — the URL is the repo's canonical one
(`README.md:40`, `website/docs/getting-started/quickstart.md:58`):

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

The installer leaves the checkout at `~/.hermes/hermes-agent` on `main`. Switch to the
concierge branch:

```bash
git -C ~/.hermes/hermes-agent fetch origin
git -C ~/.hermes/hermes-agent checkout concierge    # auto-tracks origin/concierge
```

Verify:

```bash
git -C ~/.hermes/hermes-agent log --oneline -1      # a "concierge: ..." commit
hermes --version                                    # CLI on PATH
```

## 2. Multiplex note — no config change needed

Do **not** set `gateway.multiplex_profiles`. It already defaults to `true`
(`hermes_cli/config_defaults.py:2107`, `"multiplex_profiles": True` — the plan cited
line 2095; the section has moved since). The gateway settles multiplex vs standalone
once per boot (`gateway/run.py:1814–1826`, `resolve_multiplex_mode`): with the default
on, the installed gateway becomes the single inbound process for **every** profile on
the host — default plus every live named profile under `~/.hermes/profiles/`
(`hermes_cli/profiles.py:872`, `profiles_to_serve`). That set includes the `concierge`
hub and every `bot-*` profile it provisions later.

Consequence: install **exactly one** gateway service (§5). Never also run the default
profile's gateway alongside it — two gateways double-bind the Discord token (two pollers
on one bot). The CLI actively refuses a second gateway for a profile the running
multiplexer already serves (`hermes_cli/gateway.py:4738`, exit 78).

## 3. Hub profile on the VM

Two options. **A** builds a fresh hub on the VM; **B** rsyncs the local hub profile
wholesale (it exists only after the user's local Task 5.6 gate — the local profile lives
at `~/.hermes/profiles/concierge/`).

### Option A — create fresh

```bash
hermes profile create concierge
hermes -p concierge config set concierge.enabled true
hermes -p concierge config set concierge.managed_category_ids '["<CATEGORY_ID>"]'
```

- `hermes profile create <name>` — `hermes_cli/subcommands/profile.py:18`.
- The keys are real, registered config (`hermes_cli/config_defaults.py:1484–1493`):
  `concierge.enabled`, `concierge.hub_profile`, `concierge.managed_category_ids`,
  `concierge.default_provider_group`, `concierge.provider_groups`,
  `concierge.location_channel_id`. `config set` auto-coerces the JSON list literal
  (`hermes_cli/config.py:3292`, `_coerce_config_set_value`).

Then the Discord token — a **secret: `.env` only, never config.yaml** (repo invariant:
secrets live only in `.env`):

```bash
echo 'DISCORD_BOT_TOKEN=<token>' >> ~/.hermes/profiles/concierge/.env
chmod 600 ~/.hermes/profiles/concierge/.env
```

Memory (M9): if the local hub has a Honcho config, copy it. The plugin's config chain is
`$HERMES_HOME/honcho.json -> ~/.honcho/config.json -> env`, and provisioning copies the
hub's `honcho.json` into every social bot (`concierge/provision.py`, `_install_honcho`):

```bash
# from the workstation, if the hub's honcho.json lives in the profile dir:
rsync -av ~/.hermes/profiles/concierge/honcho.json <user>@<vm>:~/.hermes/profiles/concierge/
```

Bot credentials need **no** manual step: provisioning seeds every bot profile's `.env`
from the hub's `.env` minus channel settings (`concierge/provision.py`,
`_seed_credentials`) — provider/model keys are shared, messaging tokens are not.
Whatever the hub's `.env` holds, the bots inherit at creation time.

### Option B — rsync the local hub profile wholesale

```bash
# from the workstation (profile exists after the Task 5.6 gate):
rsync -av ~/.hermes/profiles/concierge/ <user>@<vm>:~/.hermes/profiles/concierge/
```

This carries `config.yaml`, `.env` (secrets), `SOUL.md`, `honcho.json`, webhook
subscriptions, the cron store, and the channel registry in one go. Still verify the
switch is on: `hermes -p concierge config get concierge.enabled` → `true`.

### Optional — location reminders (the M10 webhook route)

Only if phone location events should reach the reminder bot. On the VM:

```bash
hermes -p concierge config set concierge.location_channel_id <CHANNEL_ID>
hermes -p concierge config set platforms.webhook.enabled true
hermes -p concierge webhook subscribe concierge-location --handler concierge-location \
    --description "phone location events -> reminder bot"
# → prints the route URL (http://<host>:8644/webhooks/concierge-location) and the
#   route token ("Secret: <route-token>") — store the token; it is printed only once
```

- The handler kind is real: `KNOWN_ROUTE_HANDLERS = {"concierge-location"}`
  (`gateway/platforms/webhook.py:57–58`); `webhook subscribe` validates against it
  (`hermes_cli/webhook.py:113`).
- **`-p concierge` matters here**: subscriptions live at
  `<hermes_home>/webhook_subscriptions.json` and the gateway reads that file from its
  *process* home — the hub-pinned unit of §5 reads the hub profile's file. (The M10
  example in `concierge/DISCORD_INTEGRATION.md` omits `-p`, which targets the default
  home — right only for a default-profile gateway.)
- The token is a secret (per-route HMAC key; the file is mode 0600). It goes into the iOS
  Shortcut and nowhere else — the Shortcut recipe is
  `concierge/DISCORD_INTEGRATION.md` § "iOS location shortcuts".

### Optional — `/connect` deep links (M8)

`/connect` and the first-message nudges link into the gateway dashboard. Links are only
built when `dashboard.public_url` is set (`hermes_cli/config_defaults.py:1029`; the
default is empty = no links, honest copy instead):

```bash
hermes -p concierge config set dashboard.public_url https://<vm-public-origin>
```

## 4. Smoke-test the hub before installing the service

```bash
hermes -p concierge gateway run
```

Expected: the gateway banner, then the Discord adapter connects (the bot shows online in
Discord). From Discord: create a channel in the managed category → the blueprint picker
appears (the Task 5.6 flow). Stop it with Ctrl+C before proceeding — the service install
starts its own gateway.

## 5. Service install (systemd)

> **Correction vs the plan.** The plan wrote `hermes -p concierge gateway install
> --system`. System-scope installs **require root** (`hermes_cli/gateway.py:2571–2573`,
> `_require_root_for_system_service`: "System gateway install requires root. Re-run with
> sudo."). Use sudo.

```bash
sudo hermes -p concierge gateway install --system
```

Expected output:

```
Installing system systemd service to: /etc/systemd/system/hermes-gateway-concierge.service
...
✓ System service installed and enabled!

Next steps:
  sudo hermes gateway start --system              # Start the service
  sudo hermes gateway status --system             # Check status
  journalctl -u hermes-gateway-concierge -f  # View logs
```

(On a TTY it also asks "Start the gateway now after installing the service?" — answer
yes; headless/SSH-piped runs default to yes.)

What the unit is and carries — `hermes_cli/AGENTS.md:216–224`, quoted:

> "Service installs are a matrix, not a unit file: `gateway.py::generate_systemd_unit(system=, run_as_user=)` (user unit AND `--system` unit with `User=`; an unresolvable `User=` is a blocker, never a dir-owner fallback), … Windows Scheduled Task and the Desktop-spawned backend all carry the profile's `HERMES_HOME` (and `HOME` for the service user) explicitly — a supervisor starts with an empty environment, so the env override that makes `-p` work interactively does not exist there."

So: the unit is named `hermes-gateway-concierge.service`
(`hermes_cli/gateway.py:2172`, `get_service_name`), runs as the invoking user
(`SUDO_USER`), and pins `HERMES_HOME=/home/<user>/.hermes/profiles/concierge` — the
service needs no shell environment. Under sudo the `-p` flag still resolves to the
invoking user's profile (`hermes_cli/main.py:573`, `_apply_profile_override`, with the
SUDO_USER fallback path).

Verify (the system scope is auto-detected when only the system unit exists —
`hermes_cli/gateway.py:3346`, `_select_systemd_scope`):

```bash
hermes -p concierge gateway status
```

Expected: `systemctl status` output plus
`✓ System gateway service is running` and
`✓ System service starts at boot without requiring systemd linger`.

## 6. Day-2 updates on the VM

> **Correction vs the plan.** The plan wrote bare `hermes update`. A bare update tracks
> `origin/main` and **switches a checkout parked on a feature branch back to `main`**
> (`website/docs/getting-started/updating.md`, "Checkout parked on a feature branch") —
> which would strand the VM on code without the concierge feature. Always pass the
> branch.

```bash
hermes update --branch concierge
```

What the pipeline does (`website/docs/getting-started/updating.md`, "What happens during
an update"; code in `hermes_cli/update_cmd.py` + `hermes_cli/update_cmd_fleet.py`):

1. **Pre-update snapshot** — a lightweight per-profile state snapshot (pairing data,
   cron jobs, `config.yaml`, `.env`, `auth.json`, …), recoverable via the snapshot
   restore flow. Best-effort; a failure warns and continues.
2. **Git pull** — pulls `origin/concierge` (because of `--branch`) and updates
   submodules.
3. **Post-pull syntax validation + auto-rollback** — compiles the critical startup
   files; on a parse failure runs `git reset --hard <pre-pull-sha>` so the install stays
   bootable, then re-executes itself on the pulled code.
4. **Dependency install** — `uv pip install -e ".[all]"`.
5. **Config migration** — prompts for new config options.
6. Desktop rebuild — not applicable on a headless VM.
7. **Gateway auto-restart, fleet-wide** — every running gateway (systemd, launchd,
   manual) is restarted onto the pulled code, drain-first: in-flight turns and cron jobs
   finish first (capped by `agent.restart_after_turn_timeout`, 30 min default), so the
   8am agenda is never cut off mid-run (`hermes_cli/update_cmd_fleet.py:1428`,
   `_restart_gateway_fleet_after_update`: "Restart every running gateway (systemd,
   launchd, manual) onto the pulled code").
8. **Version verify + receipt** — post-restart fleet version matrix and
   plan-vs-execution reconciliation; the whole run is recorded as a receipt
   (`hermes_cli/update_cmd_fleet.py:1595`, `_verify_fleet_after_update`;
   `hermes_cli/main.py`, `cmd_update` → `_finalize_update_receipt`).

Read-only preview — safe on a live fleet; inventories every running Hermes runtime
across profiles, so the concierge unit shows up with its profile:

```bash
hermes update --plan
```

**NEVER a bare `git pull` on the VM.** It skips the snapshot / restart / verify pipeline
and can strand profiles on mixed code versions (gateway processes split across old and
new code). If a manual pull ever happens, the minimum remediation is
`sudo hermes -p concierge gateway restart --system` — but prefer
`hermes update --branch concierge`, which does it correctly.

## 7. Phone-only acceptance (Task 11.2)

With the laptop fully **off**, from the Discord mobile app:

1. Create a channel in the managed category → the blueprint picker appears.
2. Spawn a **Researcher bot** → ask it something → receive a cited answer.
3. Next morning: the **reminder bot posted its 8am agenda** in its channel.

Optional VM-side verification (reading Discord is enough):

```bash
hermes -p concierge concierge list       # provisioned bots (profile names)
hermes -p concierge concierge bindings   # channel -> bot table
hermes -p <bot-profile> cron list        # schedule + last-run status per routine
hermes -p <bot-profile> cron runs        # durable execution attempts (alias: history)
```

`cron list` shows each job's schedule and last run (`last_run: ok` —
`hermes_cli/cron.py:168`); `cron runs` / `cron history` shows the execution log
(`hermes_cli/cron.py:862`). Every concierge command on the VM needs `-p concierge` (or
`-p <bot-profile>`) — without it you read the default profile's home, a different
registry and cron store entirely.

## 8. Backup & restore

Everything concierge owns lives inside `~/.hermes/profiles/`:

- `~/.hermes/profiles/concierge/` — the hub: `config.yaml`, `SOUL.md`, `.env` (secrets:
  `DISCORD_BOT_TOKEN`, provider keys), `honcho.json`,
  `webhook_subscriptions.json` (route tokens), the cron store, and
  `concierge/registry.json` — the channel→bot bindings. The registry lives at
  `<hermes_home>/concierge/registry.json` (`concierge/registry.py:91–92`); under
  `-p concierge` that is **inside** the hub profile dir.
- `~/.hermes/profiles/bot-*/` — every bot profile: its `cron/jobs.json` (the routines),
  `.env` (seeded credentials), `SOUL.md`, skills, `state.db` (sessions, memory).

Two mechanisms:

```bash
# a) on the VM — zip the critical state (config, state.db, .env, auth, cron):
hermes backup --quick        # writes ~/hermes-backup-<timestamp>.zip

# b) from the workstation — rsync the profiles tree off the VM:
rsync -av <user>@<vm>:~/.hermes/profiles/ ~/backups/concierge-vm-profiles/
```

(`hermes backup` — `hermes_cli/subcommands/backup.py`: "Back up Hermes home directory
to a zip file"; `--quick` = critical state files only.)

**Restore order** (new VM or disaster recovery):

1. Reinstall and check out the branch (§1).
2. Stop the gateway if it is running: `sudo hermes -p concierge gateway stop --system`.
3. Restore `~/.hermes/profiles/` — rsync the tree back, or unzip the backup over the
   home (the profiles dir must land with `.env` modes intact; re-run
   `chmod 600` on any `.env` that arrived world-readable).
4. Install the service (§5) and verify with `hermes -p concierge gateway status`.
5. Spot-check: `hermes -p concierge concierge bindings` lists every channel;
   `hermes -p <bot> cron list` shows the routines un-paused.

Restore the registry and the bot profiles **together**: bindings whose `profile` is
blank or malformed are skipped and those channels fall back to the hub persona
(`concierge/routing.py`, `resolve_target_profile`), and bot profiles without bindings
are inert — half a restore looks like "the bots vanished".

## 9. Troubleshooting

- **Gateway not running** — `hermes -p concierge gateway status`; if stopped:
  `sudo hermes -p concierge gateway start --system`. Logs:
  ```bash
  journalctl -u hermes-gateway-concierge -f                 # follow
  journalctl -u hermes-gateway-concierge -n 200 --no-pager  # recent lines
  ```
- **`install --system` refused: "requires root"** — run it with `sudo` (§5).
- **`gateway run`/`install` exits 78** ("The default gateway is running as a profile
  multiplexer and already serves profile 'concierge'") — a default-profile gateway is
  already running on the VM. Manage or uninstall *that* one instead of adding a second;
  two gateways double-bind the Discord token (`hermes_cli/gateway.py:4738`).
- **Bot not responding in a channel** — check the binding:
  `hermes -p concierge concierge bindings` (is the channel listed? right bot?). Then the
  routine: `hermes -p <bot> cron list` (paused? `last_run` error?). Then the unit:
  `hermes -p concierge gateway status`.
- **Registry / hub-profile mismatch after rsync** — the registry lives *inside* the hub
  profile (`~/.hermes/profiles/concierge/concierge/registry.json`); an rsync that skips
  it leaves every channel unbound (all messages fall through to the hub persona).
  Re-rsync the whole profile dir; `hermes -p concierge concierge bindings` must list
  every channel.
- **Webhook route 404 / "Webhook platform is not enabled"** — enable it:
  `hermes -p concierge config set platforms.webhook.enabled true`, restart the gateway,
  and confirm the subscription was created **with** `-p concierge` so it landed in
  `~/.hermes/profiles/concierge/webhook_subscriptions.json` — the file the hub-pinned
  gateway reads. Check with `hermes -p concierge webhook list`.
- **Update left the checkout on `main`** — re-checkout and re-run with the branch:
  ```bash
  git -C ~/.hermes/hermes-agent checkout concierge
  hermes update --branch concierge
  ```

## 10. Verification notes (plan-vs-repo corrections)

1. `hermes -p concierge gateway install --system` → **`sudo` hermes -p concierge gateway
   install --system** — system-scope installs require root
   (`hermes_cli/gateway.py:2571–2573`).
2. Day-2 `hermes update` → **`hermes update --branch concierge`** — a bare update
   switches a feature-branch checkout back to `main`
   (`website/docs/getting-started/updating.md`).
3. The multiplex default is at `hermes_cli/config_defaults.py:2107` (the plan cited
   2095; the line moved, the default `true` is unchanged).
4. The M10 webhook example in `concierge/DISCORD_INTEGRATION.md` omits `-p concierge` on
   `webhook subscribe`; on this deployment the subscribe must carry it so the
   subscription file lands in the hub profile the gateway reads
   (`hermes_cli/webhook.py:22–24`, `gateway/platforms/webhook.py:400–401`).
