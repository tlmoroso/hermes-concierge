# Discord surface facts

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
