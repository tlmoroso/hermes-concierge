# Discord integration design note (Task 5.1 spike)

**Question this spike answers:** where, exactly, does an inbound Discord message become a
profile-bound agent turn, and what is the narrowest seam for routing "this channel's messages
run as *that* bot profile"?

**Method:** code trace only (no code changes). Every claim carries a `file:line` citation
against the working tree at the time of the trace (branch `concierge`, HEAD `322a452`).
Line numbers drift; anchor on the cited function/identifier when they do.

---

## (a) The inbound path, end to end

One Discord connection → one `DiscordAdapter` → one multiplexed gateway runner → a
profile-scoped turn. The full chain:

1. **Event registration** — `plugins/platforms/discord/adapter.py:1311` (`on_message`), inside
   `DiscordAdapter.connect()` (adapter.py:1220). The adapter registers
   `on_message`, `on_message_edit`, `on_message_delete`, `on_thread_create`,
   `on_thread_update`, `on_voice_state_update` (adapter.py:1311–1336).
   **`on_guild_channel_create` is NOT registered** — the hook point for "channel created in a
   managed category" does not exist yet (Task 5.5 adds it next to these).
2. **Admission** — `_dispatch_discord_message` (adapter.py:1513) awaits `_ready_event`, then
   `_discord_message_admission(message, claim=True)` (adapter.py:1432) applies ingress policy:
   self-message drop, bot-author gate (`allow_bots`, default `none` — adapter.py:4890–4893),
   allowlist/mention checks.
3. **`_handle_message`** (adapter.py:5896) — channel policy (`allowed_channels`/`ignored`/
   `free_response`/`require_mention`), **auto-thread** (adapter.py:5958–5992, `_auto_create_thread`
   at adapter.py:5294), and then the critical call:
   **`source = self.build_source(...)` at adapter.py:6037**, passing
   `chat_id`, `thread_id`, `guild_id`, `parent_chat_id` (which also feeds `profile_routes`
   matching — see (b)).
4. **`build_source`** (`gateway/platforms/base.py:4548`) — **the single ingress chokepoint where
   per-channel profile routing already happens.** With `gateway.profile_routes` configured, it
   calls `gateway_runner._profile_name_for_source(...)` (base.py:4576) and stamps the matched
   profile onto `source.profile` (base.py:4585). `source._transport_adapter_ref` and
   `source.profile_route_rejected` are stamped at base.py:4588–4589.
5. **Batching vs. dispatch** — `_handle_message` ends at adapter.py:6122–6123: plain-text events
   with batching enabled go to `_enqueue_text_event` (base.py:2441); everything else calls
   `await self.handle_message(event)` (base.py:3887).
6. **`handle_message` → the runner** — base.py:3887 canonicalizes identity FIRST
   (`_canonicalize`, base.py:2322 → `gateway/session_identity.py:131` `canonical_identity` →
   `resolve_identity`, session_identity.py:191), drops rejected routes (`_drop_unresolved`,
   base.py:2346; also at base.py:3907 before `_event_session_key` at base.py:3915 — session keys
   are profile-namespaced via `_session_key_profile`, base.py:2360), then spawns the turn as a
   background task (`_start_session_processing`, base.py:3799 → `_process_message_background`,
   base.py:4371) which calls the installed
   message handler (`self._message_handler`, set at base.py:2227–2229, invoked at base.py:4387).
7. **The handler IS the profile seam** — `gateway/run_adapters.py:1096` `_wire_adapter_handlers`
   installs `self._primary_message_handler()` (run_adapters.py:1454). Under multiplex this is
   `_make_default_profile_message_handler` (**run_adapters.py:1412–1424**):
   ```python
   profile_home = self._admit_primary_source(event.source, default_home) or default_home
   async with _async_profile_runtime_scope(profile_home):
       return await self._handle_message(event)
   ```
   `_admit_primary_source` (run_adapters.py:1445) → `_canonicalize` (run_adapters.py:1364) →
   identity's `runtime_home` (session_identity.py:265–267). **So the answer to "which method
   resolves the agent/session for an inbound message" is: `resolve_identity`
   (gateway/session_identity.py:191), called from `BasePlatformAdapter.build_source`
   (base.py:4576) and again at every ingress gate, with the turn itself executed inside
   `_async_profile_runtime_scope(profile_home)` (run_adapters.py:1421).**
8. **The turn itself** — `gateway/run_inbound.py:1259` `_handle_message`: admission
   (`_hm_admit_event`, run_inbound.py:160 — re-canonicalizes, drops `profile_route_rejected`
   fail-closed at run_inbound.py:183–184, runs the `pre_gateway_dispatch` hook at
   run_inbound.py:66/225), command dispatch,
   session claim, then `_handle_message_with_agent` (gateway/run_turn.py:2126).
9. **Auth stays with the transport** — `gateway/run.py:4035` `_is_user_authorized_for_source` /
   run.py:4052 `_under_authorization_profile`: authorization runs under the **transport** home
   (the hub profile that owns the Discord token), NOT the routed runtime home — by design,
   "the routed runtime need not copy the shared bot token/allowlist" (run.py:4038–4041).
   This is exactly the concierge's credential model: hub owns `DISCORD_BOT_TOKEN`; bots get
   provider keys only (`concierge/provision.py` `create_bot`, M4).

**History-backfill caveat:** a batched text event (`_enqueue_text_event`, base.py:2441) is
flushed later via the same handler chain, so the routing decision made in `build_source` is
the one that sticks — the binding verdict is evaluated per event at ingress (adapter.py:6037),
not at flush time.

**Recovered messages:** `_dispatch_recovered_message` (adapter.py:2314) re-runs admission and
`_handle_message(recovered=True)` — same path, same routing.

---

## (b) The room-dispatch precedent

The plan cites `gateway/platforms/api_server_room_dispatch.py` as the routing precedent. The
trace shows it is a *narrower* precedent than the plan assumes — it is scoped RoomLink
validation (HMAC grant verify, idempotency keys, hidden-session creation:
`_ensure_hosted_member_session` at api_server_room_dispatch.py:21, `_normalize_room_dispatch`
at api_server_room_dispatch.py:71), **not** a generic per-channel profile-routing mechanism.
The precedent to copy for per-channel routing is instead the **`gateway.profile_routes`
config mechanism**, which the Discord path already exercises end to end:

- **Config:** `gateway.profile_routes` — `[{name, platform, profile, chat_id | guild_id |
  thread_id | user_id, bot_profile?, enabled?}]` (hermes_cli/config_defaults.py:2115–2117;
  parsed by `parse_profile_routes`, gateway/profile_routing.py:133; loaded into
  `GatewayConfig.profile_routes`, gateway/config.py:793; validated profile names, sorted
  most-specific-first by `ProfileRoute.specificity` — user_id 16 > thread_id 8 > chat_id 4 >
  guild_id 2, profile_routing.py:69–71).
- **Matching:** `match_profile_route` (profile_routing.py:174) / `ProfileRoute.matches`
  (profile_routing.py:74): conjunctive AND, `chat_id` matches the channel **or the parent of
  a thread/forum post** (`parent_chat_id`, profile_routing.py:84–93 — a channel route covers
  every thread/post under it), and `bot_profile` gates which receiving bot's routes apply
  (profile_routing.py:94; `None` = the default profile's shared bot — i.e. the concierge hub).
- **Where it's read:** `GatewayRunner._profile_name_for_source` (gateway/run.py:4344–4395) —
  gated on `multiplex_profiles` (config_defaults.py:2104, default **true**), rejects
  fail-closed when the target profile isn't served (`ProfileRouteRejected` → the message is
  dropped with one WARNING, never re-homed to `agent:main`: run.py:4378–4391, and the drop
  sites at base.py:2346/3907, run_inbound.py:176–184).
- **What it yields:** `source.profile` (stamped in `build_source`, base.py:4576–4585) →
  `RoutingIdentity.runtime_profile`/`runtime_home` (session_identity.py:262–271) → the turn
  runs inside `_async_profile_runtime_scope(runtime_home)` (run_adapters.py:1421), which is
  `gateway/run.py:1773` `_profile_runtime_scope`: `set_hermes_home_override` (contextvar) +
  `set_secret_scope` (per-profile `.env` credential source, no `os.environ` mutation) +
  the profile's full terminal policy. **This is the (b) answer: room dispatch's
  per-profile turn execution is exactly this same scope; what api_server_room_dispatch.py
  adds on top is grant verification, not a different run mechanism.**

### What this means for per-channel bot routing (the recommendation)

**The recommended seam is: keep routing decisions in `concierge/routing.py` + a
`profile_routes`-shaped binding, and re-home the message by stamping `source.profile` at
`build_source` time — not by wrapping `_handle_message` a second way.**

Concretely:

1. **Route lookup** — the concierge mixin's `_concierge_profile_for_message` (Task 5.3)
   should resolve the channel → bot binding from `concierge/registry.json` and the verdict
   must be applied **before `build_source` returns** (adapter.py:6037), because every later
   stage (session key at base.py:3915, scope at run_adapters.py:1421) reads the identity
   pinned there. The clean wire-up: the mixin either (i) feeds the binding as a route-shaped
   entry so `_profile_name_for_source` finds it, or (ii) overrides
   `_profile_name_for_source`'s input by pre-stamping `message.channel` facts into the
   `build_source` call — but **it must not add a second `_profile_runtime_scope` wrapper
   around the turn**: `_make_default_profile_message_handler` (run_adapters.py:1412) already
   scopes every turn to the identity's `runtime_home`. A second mechanism would fight the
   session-key namespacing (`agent:<profile>:...`, base.py:2360–2379) and the
   busy-session twin (run_adapters.py:1426) — both of which consult the same identity.
   In short: **route at `build_source`, let the existing scope do the work.**
   - Fallback if (i)/(ii) prove brittle (the plan's own risk note): the Bot Mode envelope
     relay (`tools/bot_relay.py`) remains the fallback, at the cost of latency.
2. **The registry-vs-config question.** `profile_routes` is boot-time config
   (`load_gateway_config`, gateway/config.py:817, called from `load_gateway_config_for_runner`,
   gateway/run.py:1813; no SIGHUP/watcher reload exists — a route added to config.yaml after
   boot is invisible until restart). The concierge needs **runtime-mutable** bindings
   (`/bot new` on a phone must take effect immediately). Two workable shapes, in preference
   order:
   - **(preferred) Keep `concierge/registry.json` as the source of truth** and resolve the
     binding in the adapter before/at `build_source` (the pure `resolve_target_profile` from
     Task 5.2 stays the decision function). Serve-set membership is NOT a problem:
     `_multiplex_profile_homes` (run.py:1668) calls `profiles_to_serve(multiplex=True)`
     (hermes_cli/profiles.py:872–885) which is **a pure directory read** — a freshly
     created `profiles/bot-reminders-a1b2/` is served immediately, no restart.
     The only gap: the routing *decision* must come from the registry, not from the
     boot-loaded `config.profile_routes` list.
   - (alternative) Provisioning writes a `profile_routes` entry per binding and the
     gateway reloads config; rejected — no reload path exists, and mutation of user
     config.yaml per bot is invasive.
3. **Bot-authored loop safety is already structural.** The default `allow_bots: none`
   (adapter.py:4890–4893) means bot-authored messages (including the hub's own replies and
   every member bot's replies) never even reach `_handle_message` — the anti-loop guarantee
   of Task 5.2's `resolve_target_profile(author_is_bot=...)` is enforced twice: once by
   admission (adapter.py:1449–1459), once by the routing function. Note `allow_bots` is
   per-profile via the scoped env read (`DISCORD_ALLOW_BOTS` under the hub's secret scope) —
   the concierge hub must keep it `none`/unset; if a room needs bot-to-bot turns (M7), that
   is the config surface to revisit, with the debounce continuation
   (`_is_bot_tag_debounce_continuation`, adapter.py:1444) as the existing carve-out.
4. **Delivery of routine outputs into the bound channel already works.** `create_job` is
   called with `origin={"platform": "discord", "chat_id": channel_id}` +
   `deliver="origin"` (concierge/provision.py:449–461), the minimum
   `cron/scheduler_delivery.py::_resolve_origin` (scheduler_delivery.py:83–100) accepts.
   For a bot profile with **no adapter of its own**, `_drain_restart_safe_cron_deliveries`
   (run.py:4637–4656) wraps `SharedRouteAdapters` (cron/scheduler_preflight.py:193+) —
   the delivery drain consults `ProfileRoute.matches` against the **primary** home's
   config.yaml `profile_routes` (`_primary_profile_routes_for_current_home`,
   scheduler_preflight.py:137) to lend the hub's Discord adapter to a satellite profile's
   delivery. **Consequence:** a one-line `profile_routes` entry per bot (in the HUB's
   config.yaml, not the bot's) is likely REQUIRED for routine delivery to reach the channel,
   even when inbound routing resolves from the registry. Task 5.3/7.3 must verify this in
   the Task 5.6 E2E and, if needed, provisioning should append this entry at bind time
   (it is idempotent config mutation of the hub's own file — acceptable, and the
   scheduler re-reads it per drain, scheduler_preflight.py:150–177, so it is live without
   restart). This is an OPEN ITEM flagged for the Task 5.3 implementer, not settled here.
5. **`bot_profile` scoping:** a `profile_routes` entry only applies to messages received by
   the bot of its `bot_profile` (`None` = the default/hub's shared bot —
   profile_routing.py:94, run.py:4348–4355). Since the concierge runs ONE Discord app owned
   by the hub (plan assumption 2), hub-side entries need no `bot_profile` key.
6. **M7 rooms need no new run mechanism.** Sequential member turns reuse the same
   identity→scope path per member: each member's turn constructs its own event/source with
   `source.profile` = member profile (mirroring how `_handle_message` builds the source at
   adapter.py:6037), then runs under the existing handler — which scopes to that member's
   `runtime_home`. The desktop room precedent (`tui_gateway/hosted_room_driver.py`,
   `gateway/platforms/api_server_room_grants.py`) confirms turns queue behind the active
   member; the Discord variant just swaps the transcript window for channel history
   (the `channel_context` backfill — adapter.py:6061–6090, fetched by `_fetch_channel_context`
   at adapter.py:5035 — is the pattern to reuse per member).

---

## (c) Channel-create / forum-post hooks

- **Forum post = thread**: a forum post creates a `Thread` object, delivered as
  `on_thread_create` — **already registered** (adapter.py:1324–1325) and normalized into
  `gateway_platform_event` `event_type="thread_created"` by `_on_platform_thread_create`
  (adapter.py:1649–1657, payload via `_thread_event_parts`, adapter.py:1606: `thread_id`,
  `parent_chat_id` = the forum channel, `guild_id`, `name`, `owner_id`).
  BUT that pipeline is **observer-only** (`_emit_platform_event`, adapter.py:1569, gated on
  `has_hook("gateway_platform_event")`; consumed at run_adapters.py:1480–1486 via
  `invoke_hook`, registered in `hermes_cli/plugins.py:190`; envelope contract:
  `hermes_cli/plugins_dispatch.py:200` — plugins get the normalized dict, never adapter
  handles, and **cannot reply into the channel** through it). So for the Task 5.5
  "auto-post the blueprint picker" flow the mixin needs its OWN
  `@self._client.event on_thread_create` handler — the existing registration would need
  coordinating (discord.py supports one handler per event name; a second `@event` decorator
  on the same client REPLACES the first — the mixin must instead wrap/chain the existing
  `_on_platform_thread_create` or the registration site must call both). Cleanest: register
  one concierge event handler at the same registration block (adapter.py:1311–1336) guarded
  by `concierge.enabled`, and have it call both the concierge logic and the existing
  normalizer.
- **Guild channel create**: NOT registered anywhere (verified: no
  `on_guild_channel_create` in the adapter). Task 5.5 adds
  `@self._client.event on_guild_channel_create(channel)` at the same registration block;
  `channel.category_id`/`channel.guild.id` are the facts to check against
  `concierge.managed_category_ids`; the picker is posted with `channel.send(...)`.
- **Slash commands** for `/bot`/`/room`: `_register_slash_commands` (adapter.py:4402) and
  the bulk `tree.sync()` at adapter.py:2097 — as the plan states. The adapter already builds
  interaction-originated MessageEvents through the same `build_source` → `handle_message`
  path (adapter.py:4652, 4712, 4724), with guild_id/parent_chat_id fed to route matching
  (#69178, adapter.py:4646–4649), so slash-command turns route identically to message turns.

---

## Verdict: VALIDATED

### What worked
- The full inbound path is traced with zero code changes; every load-bearing claim has a
  file:line citation.
- A complete per-channel profile-routing mechanism **already exists** (`profile_routes` +
  `RoutingIdentity` + `_profile_runtime_scope`) and is exercised by Discord messages today.

### What didn't / surprises
- `api_server_room_dispatch.py` is NOT the precedent the plan thought — it is RoomLink grant
  validation. The real precedent (`profile_routes`) is better: it already stamps
  `source.profile` in the Discord adapter's own `build_source` call, so per-channel bot
  routing needs **no new turn-execution mechanism at all**.
- The plan's Task 5.3 sketch ("run the turn under `_profile_runtime_scope` exactly the way
  `api_server_room_dispatch.py` does") would create a SECOND scope mechanism fighting the
  existing one (`_make_default_profile_message_handler` already scopes every turn).
- discord.py's one-handler-per-event registration means the Task 5.5 `on_thread_create`
  hook cannot just add another `@self._client.event` — it must chain with the existing
  `_on_platform_thread_create` normalizer.

### Recommendation for the real build (Task 5.3)
1. Resolve the binding in the adapter **before/at `build_source`** (registry lookup via the
   pure `resolve_target_profile`; `source.profile` carries the verdict) — the existing
   identity → scope machinery executes the turn under the bot's home with no wrapper added.
2. Authorization is already transport-scoped (run.py:4035): keep it that way — the hub's
   allowlist governs admission for ALL bots' channels, so `/bot` provisioning does not need
   per-bot user allowlists for v1.
3. Session isolation comes free: profile-namespaced session keys (base.py:2360) mean each
   bot's transcript is its own session per channel — no extra work in the mixin.
4. Keep `allow_bots` at `none` on the hub; bot-authored messages then cannot route (anti-loop).
5. Open item (verify in Task 5.6 E2E): hub-side `profile_routes` entry per bot may be needed
   for **outbound routine delivery** (`SharedRouteAdapters` lending in
   run.py:4653–4656); provisioning should append it at bind time if confirmed.
6. For M7 rooms: per-member events stamped with `source.profile` = member, executed through
   the standard handler — sequential turns, Discord channel as transcript, reusing the
   existing `channel_context` backfill pattern (adapter.py:6075) for member history windows.

## Live setup (multiplex launchd topology) — where the `concierge:` section lives

Task 5.6's original commands assumed a standalone hub gateway (`hermes -p concierge
gateway run`), where the hub profile's config.yaml IS the runner config. On the
standard always-on topology — one launchd multiplex gateway from the default root
(`ai.hermes.gateway`, HERMES_HOME=~/.hermes, the Discord adapter owned by the
`concierge` profile) — `load_gateway_config_for_runner()` reloads the process-level
GatewayConfig scoped to the DEFAULT root, so every `concierge:` key the surfaces
read (`enabled`, `managed_category_ids`, `location_channel_id`, `provider_groups`,
`default_provider_group`) is bridged from **`~/.hermes/config.yaml`**, not from
`profiles/concierge/config.yaml`. The Discord event/command gate
(`_concierge_events_wanted` → `runner.config.concierge`), the auto-offer settings,
and the webhook location handler all read that bridge; a `concierge:` section that
exists only in the profile config is a silent no-op (no events, no `/bot` group, no
routing — verified live 2026-09-27).

```bash
hermes config set concierge.enabled true
hermes config set concierge.managed_category_ids '["<CATEGORY_ID>"]'
hermes gateway restart
```

The hub profile's own state — registry (`<profile>/concierge/registry.json`),
spawned bot profiles, cron stores — still lives under `profiles/concierge/` (those
paths resolve from `get_hermes_home()` inside the concierge profile's scope), and
CLI access to them needs `-p concierge`.

Discord credential ownership: exactly one profile's `.env` holds DISCORD_BOT_TOKEN
(a duplicate is refused at boot by the token lock). The concierge hub's adapter
authorizes senders from its own scoped `.env` — `DISCORD_ALLOWED_USERS`,
`DISCORD_HOME_CHANNEL`(_THREAD_ID) move there with the token; under multiplex a
secondary adapter never sees the default home's `.env`.

## Deferred follow-ups

- Same-fan-out sibling visibility: room members see sibling replies from EARLIER rounds (the
  shared history window), but not replies posted during the SAME fan-out — the window is fetched
  once, before the first member dispatches. Desktop-room parity (seeing the current round's
  earlier member replies) would need driver-side reply capture between member turns.

## iOS location shortcuts (Task 10.1/10.2)

Design: an iOS **Shortcuts automation** ("When I arrive at / leave `<location>`") POSTs a small
JSON payload to the gateway's `concierge-location` webhook route. The route kind
(`handler: concierge-location`, dispatched in `gateway/platforms/webhook.py` next to the
`cron_job` / `deliver_only` kinds) resolves the reminder bot bound to
`concierge.location_channel_id` (`concierge/location_events.py::resolve_location_run`) and
runs the location prompt **as that bot** (`source.profile`), delivering its reply to the bound
Discord channel through the hub's live adapters — the same origin destination
(`{'platform': 'discord', 'chat_id': <channel>}`) the bot's cron routine uses. No laptop
needed; the phone is the location sensor.

### 1) One-time gateway setup (on the hub)

```bash
# a) name the channel whose bound bot receives location events (the reminder
#    bot's channel — see /bot list for the id, or right-click the channel →
#    Copy Channel ID in Discord with Developer Mode on):
hermes -p concierge config set concierge.location_channel_id <CHANNEL_ID>

# b) create the route (auto-generates and prints the route token):
hermes webhook subscribe concierge-location --handler concierge-location \
    --description "phone location events -> reminder bot"
#    → prints e.g.  URL: http://<gateway-host>:8644/webhooks/concierge-location
#                   Secret: <route-token>

# c) the webhook platform must be enabled (platforms.webhook.enabled) and the
#    gateway running: hermes -p concierge gateway run
```

The route token (the route's HMAC `secret`) lives in `<hermes_home>/webhook_subscriptions.json`
(0600) and is printed only by `hermes webhook subscribe`, at creation — `hermes webhook list`
never shows it (it prints URL/Profile/Events/Deliver only). Store the token when you create the
route; if it is lost, rotate it by re-subscribing with a fresh `--secret` (subscribe prints the
new token). It is a **secret**: it lives in the
subscriptions file and the Shortcut, nowhere else — never in config.yaml or the repo.

### 2) The iOS Shortcut (on the phone)

Shortcuts app → **Automation** tab → **+** → **Personal Automation**:

1. **When I Arrive** (or **When I Leave**) → pick or drop a pin for the location → **Run
   Immediately** (optionally disable "Ask Before Running" so no tap is needed).
2. Add action **Get Contents of URL**:
   - URL: `https://<gateway-host>:8644/webhooks/concierge-location`
   - Method: **POST**
   - Request Body: **JSON**, one dictionary:
     ```json
     {
       "event_type": "location",
       "user": "your-name",
       "region": "work",
       "event": "enter"
     }
     ```
     `region` is a free-form label you choose (match it in your reminders, e.g. "work");
     `event` is `"enter"` for arrival automations and `"exit"` for departure ones.
   - Headers (expand the request): add header **X-Gitlab-Token** = `<route-token>` from step 1.
     This is the webhook's plain shared-token auth mode — iOS Shortcuts cannot compute HMAC
     signatures, but it can send a static header; the validator compares it timing-safely
     against the route's secret. (Full HMAC modes — `X-Hub-Signature-256`,
     `X-Webhook-Signature-V2` — also work if your client can compute them.)
3. Repeat for each region you care about (arrive home, leave work, ...).

When it fires: the gateway looks up the bot bound to `concierge.location_channel_id`; the bot
runs the prompt ("The user just arrived at work. Deliver any work-related reminders now,
briefly.") with its own soul/skills and posts its reply in the bound channel. Unset channel,
unbound channel, or a garbage payload → the event is ignored (HTTP 200, `"status":
"ignored"`); a wrong token → refused (401).

### 3) Mac fallback (documentation only — not implemented)

When the laptop is on and the phone's automations are undesirable, the same webhook can be
driven from the Mac's **local default profile** with a cron job that uses the `apple/findmy`
skill to watch named regions and POST the same JSON body to the route (with the same
`X-Gitlab-Token` header). Sketch, if ever requested:

- a cron job in the local default profile, schedule `*/5 * * * *`;
- its monitor script runs a small `findmy` region check and exits non-zero when no
  region boundary was crossed since the last tick (unchanged output suppresses the run);
- the prompt tells the agent to POST
  `{"user": ..., "region": ..., "event": "enter"|"exit"}` to
  `http://<gateway>/webhooks/concierge-location` with the route token.

The cloud gateway is the receiver either way; the Mac is only another sensor.
