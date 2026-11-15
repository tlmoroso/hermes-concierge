# Debugging playbook

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
