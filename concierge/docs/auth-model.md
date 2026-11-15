# Auth model (Nous Portal)

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
