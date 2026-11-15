# Deployment topology

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
