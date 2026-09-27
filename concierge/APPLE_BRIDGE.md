# Apple data bridge — options (Task 10.3)

**Problem:** the Apple-only skills (`apple-reminders` via `remindctl`, `apple-notes`, and
`apple/findmy`) read Mac-local app databases and cannot run on the Linux VM the cloud
concierge deploys to (M11). The *data*, though, is reachable three ways. Pick per
deployment; **(a) ships for launch, (b) is the backup**.

The consumer is the reminder bot (and the scheduler bot for calendar reads):
see `concierge/blueprints.py` — the platform-neutral `todo` toolset is the reminder
store, and this bridge is how Apple-native surfaces (Reminders.app, iCloud Calendar)
stay in sync with it.

---

## (a) iCloud CalDAV — RECOMMENDED for launch

Apple Reminders **and** iCloud Calendar both sync over CalDAV, so a client on the Linux
VM reads and writes them directly. No Mac, no phone relay, no laptop.

- **Server:** `https://caldav.icloud.com/` — calendar collections AND the reminders
  (VTODO) collections live under the same principal.
- **Auth:** Apple ID (full email) + an **app-specific password** —
  appleid.apple.com → Sign-In and Security → App-Specific Passwords (requires 2FA
  on the account). This is a scoped, revocable credential, **not** the iCloud login
  password. Revoke/reissue it freely; it only ever grants CalDAV.
- **Where the secret lives:** the VM profile's `.env` (secrets are .env-only; never
  config.yaml, never the repo):
  ```
  ICLOUD_USERNAME=user@example.com
  ICLOUD_APP_PASSWORD=abcd-efgh-ijkl-mnop
  ```
- **Code:** `concierge/caldav.py` — a minimal RFC 5545/4791 client: `list_today_events`
  (calendar-query time-range REPORT), `list_incomplete_reminders` (VTODO without
  COMPLETED), `create_reminder` (POST a VTODO). `client_from_env()` wires the
  credentials from the two env vars above.
- **Dependency note:** no CalDAV library is in this repo's dependencies
  (pyproject.toml/uv.lock carry neither `caldav` nor `icalendar`), and we do not
  vendor an implementation — the module is pure parsing/formatting plus a pluggable
  transport (`CalDAVTransport`; default `RequestsTransport` over the core-pinned
  `requests`). If discovery or server-quirk robustness outgrows it, adopt the
  `caldav` library behind the same transport seam (an extras group, lazily
  installed); the fixture-based parsing/formatting tests keep either honest.
- **Bot usage:** the scheduler bot reads today's events through this instead of
  Google; the reminder bot can mirror its to-dos into Apple Reminders so they show
  up natively on the phone.

## (b) iPhone-as-bridge — the sync backup

A daily iOS Shortcut pushes today's reminders into the concierge — zero new
credentials (only the webhook route token) and the phone doubles as the location
sensor (see DISCORD_INTEGRATION.md's "iOS location shortcuts").

- Automation → Time of Day (e.g. each morning) → **Find Reminders** (filter to a
  list) → **Get Contents of URL** POST JSON to
  `https://<gateway>:8644/webhooks/concierge-location` — the same route kind as
  Task 10.1 — with the `X-Gitlab-Token` header carrying the route token.
- The body shape is the location route's, with the reminder payload riding along:
  `{"event_type": "reminders-sync", "user": "...", "reminders": [{"title": "...",
  "due": "..."}]}` — a small route-handler extension maps it onto the reminder
  bot's prompt (NOT implemented yet; the route exists and auth is identical).
- No credentials beyond the route token ever leave the phone.

## (c) Local Mac bridge — document only (not implemented)

Keep `apple-reminders` / `apple-findmy` on the Mac's **local default profile**
(the skills need macOS and the laptop's app databases) and let the cloud bots
reach them via webhook when the laptop is awake:

- a cron job in the local default profile runs the apple-reminders skill on a
  schedule and POSTs a digest to a gateway webhook route (the Task 10.1
  mechanism, any route kind);
- the findmy variant is the Mac location fallback already sketched in
  DISCORD_INTEGRATION.md ("Mac fallback").

**Tradeoff:** the bridge only works while the laptop is awake — it is a
laptop-hours augmentation of (a)/(b), never the primary path. Do not build it
unless (a) and (b) both fail for a user.

---

## Decision for launch

Ship **(a) iCloud CalDAV** on the VM: always-on, no laptop, no phone relay, and
the credential is a revocable app-specific password. Keep **(b)** as the sync
backup for users who will not put an app-specific password on the VM. **(c)**
stays documentation-only.
