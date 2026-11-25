# Open threads & backlog

1. **Notes Bot in the cloud — vault + sync BUILT and DEPLOYED** (verified
   2026-11-25). Zettelkasten method, Syncthing sync (multi-user reproducible),
   neutral vault path `/root/hermes-vault` VM / `~/hermes-vault` Mac, NO
   pre-defined sections. Syncthing v1.27.2 running, Mac device connected,
   folder in sync, `OBSIDIAN_VAULT_PATH` wired through the hub `.env`. Spec:
   `concierge/handoffs/NOTES_BOT_HANDOFF.md`; deployed state:
   `concierge/APPLE_BRIDGE.md`. REMAINING (operator-side): spawn the Notes Bot
   from Discord (managed category -> picker -> Notes) — no notes bot is
   provisioned yet, so the synced vault has no consumer.
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
