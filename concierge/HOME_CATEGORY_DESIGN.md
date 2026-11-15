# Home Category Design — per-user private spaces + thread-first replies

Status: **DRAFT, awaiting operator confirmation (2026-11-14).** NOT built.

Builds on `ORCHESTRATOR_DESIGN.md` (the orchestrator front door) and implements
`MULTI_USER_PLAN.md` §2.1 ("Private spaces, not shared channels"), which is currently
the largest unbuilt piece of that plan.

## 0. Operator decisions

**Confirmed 2026-11-14** (operator answers, verbatim intent captured in §12):

1. **Admins create the category; the user chooses public vs private.** The admin
   runs the command; a `--public` flag (default: private) sets the permission
   overwrites. Everything the user owns is created inside it thereafter.
2. **One home category per Discord user**, recorded in the registry `users`
   section. It is the single placement authority: general channel, worker
   channels, and anything else the orchestrator spawns.
3. **The general channel in that category is the primary orchestrator surface.**
4. **The orchestrator is reachable from anywhere unbound.** Typing in an unbound
   channel in a *different* public category still routes to the author's
   orchestrator, which reuses that user's existing worker bots and creates any
   new worker in the home category.
5. **Thread-first replies apply to the WHOLE concierge surface, not just the
   home category** (operator, this turn). Every bot-owned / free-response
   channel — bindings, rooms, managed-category channels, home categories —
   answers in a thread. The channel becomes an index of tasks; the thread
   holds the intermediate chatter. See §5.

## 1. Model

"Orchestrator owns the user; the **home category** is their space."

- Every admitted user gets ONE Discord category (their home category) plus one
  general channel inside it, bound to their orchestrator.
- Worker bots are per-user. Their channels live in the **home category**
  regardless of which channel the request was typed in.
- Threads are the unit of work inside a bot's channel: one task = one thread =
  one session. The channel becomes an overview; the thread holds the
  intermediate chatter.

The current placement rule (`_concierge_event_category_id(event)`, mixin `:874`)
is "the trigger channel's category". That is what this design replaces, for two
reasons: it places workers wherever the user happened to be typing, and it
**silently fails inside threads**, which is the root of the thread problem (§5).

## 2. Registry v3 — add `home_category`

`concierge/registry.py`. Same pattern as the `users` addition: additive section
field, v1/v2 registries load fine.

```json
"users": {
  "<discord_user_id>": {
    "orchestrator":  "bot-orchestrator-<suffix>",
    "home_category": "<category_id>",
    "home_channel":  "<channel_id>",
    "guild_id":      "<guild_id>",
    "workers":       { "<blueprint_key>": "<profile>" },
    "created":       "<iso>"
  }
}
```

- `_USER_MUTABLE_FIELDS` (`:613`) gains `"home_category"` — the closed set exists
  so a typo'd field raises instead of silently dropping the update.
- `_normalize_user_entry` (`:638`) gains a tolerant read (non-blank str, else
  `None`), matching `home_channel`. A hand-edited registry must never crash a
  reader, and the next save self-heals.
- New `home_category_for_user(user_id)`, sibling to `orchestrator_for_user`
  (`:728`) and `home_channel_for_user`: same tolerant read, no I/O of its own
  beyond the load the caller already performed where possible.
- `set_user` / `update_user` accept `home_category`.
- **`clear_orchestrator` (`:791`) must NOT clear `home_category`** — it currently
  preserves `workers`/`guild_id`/`created` and clears `orchestrator` +
  `home_channel`. Deleting a category in Discord must not orphan the record of
  the user's home channel either; keep `home_category` as a durable pointer and
  let the reconciliation step (§6) decide whether it is still valid.

## 3. Admin surface

### 3.1 Slash: extend `/bot category <user>`

`_concierge_cmd_category` (mixin `:2340`) already exists, is admin-gated in the
slash wrapper (`:2230`, `concierge/auth_tiers.is_admin`), and creates
`<display-name>-private` with the user + all guild admins given
`view_channel` + `manage_channels`. Extend it to:

1. Accept `visibility: "private" | "public"` (default `private`).
   - `private` = today's overwrite set (default role denied, user + admins allowed).
   - `public` = no overwrite on the default role; optionally `manage_channels`
     for the user so they can still add channels.
2. Create the **general channel** inside the new category
   (`_concierge_create_channel`, `:2655`).
3. Provision the orchestrator if the user has none
   (`provision.create_bot("orchestrator", user_id=…, channel_id=…)`).
4. Record `home_category` + `home_channel` + `guild_id` in the registry
   (`set_user` / `update_user`), then `bind_channel`.
5. Suppress the auto-offer on the created channels (already done by
   `_concierge_create_channel` via `_concierge_remember_hub_created_channel`).
6. Reply ephemerally with both ids (today it reports only the category).

Idempotency: re-running for a user who already has a home category adopts the
existing one (verify it still exists; see §6) rather than creating a duplicate.

### 3.2 CLI

`hermes concierge new orchestrator --user <id>` gains `--category <id>` (bind to an
existing category, create the general channel in it) and `--public`. The existing
`--channel` XOR `--user` rule stays: `--category` is only legal with `--user`.

## 4. Routing — the home category is the authority

Today the unbound→orchestrator lane is gated on
`_concierge_event_in_managed_category(event)` (`:905`), which resolves the
category from the raw channel only.

New decision function `_concierge_event_in_scope(event) -> bool`:

1. `category_id = _concierge_event_category_id(event)` (`:874`), extended to
   resolve **through the parent when the raw channel is a thread**: if the raw
   channel exposes `parent_id` (discord.py: `Thread.parent_id`), resolve the
   parent channel object via `client.get_channel(...)`
   (`_concierge_dispatch_channel_object` already does exactly this shape,
   `:1323`) and read its `category_id`. One hop — never an unbounded walk.
   `SessionSource` already carries `parent_chat_id`.
2. `category_id in managed_category_ids` → in scope (today's behavior).
3. Otherwise, if the AUTHOR has a live orchestrator whose registry entry carries
   a `home_category` → in scope **when `unbound_scope: "guild"`** (default
   `"managed"`, i.e. today's behavior; see §8).

Fail-closed exactly as today: an unresolvable category or an unreadable registry
resolves False and the hub answers.

**Branches (b) and (c) of `_concierge_dynamic_runtime_profile` (`:2074`) are
unchanged in kind** — the stamp is still the author's registry orchestrator and
admission still re-confirms it. Only the *category* half of the gate changes, so
bindings and rooms still win and a spoofed stamp still dies.

## 5. Thread-first replies — the whole concierge surface

Confirmation (operator, this turn): **not home-category-only.** Wherever the
concierge owns the conversation, the reply goes in a thread. The channel becomes
an index of tasks; the thread holds the intermediate chatter.

### 5.1 The surface (the existing free-response union)

The concierge already computes exactly this set, for the mention gate:

- `_concierge_free_response_channels()` (`:1988`) — bound channels + room channels.
- `_concierge_free_response_message()` (`:2009`) — any channel in a managed category.

Threading keys off the same union, **plus** a user's `home_category` even when
that category is not listed in `managed_category_ids`. ONE predicate decides
both the mention gate and the threading gate, so the two cannot drift:

```
def _concierge_owns_conversation(self, message, source) -> bool
```

### 5.2 The seam

The threading gate lives inline in `DiscordAdapter._handle_message`
(`adapter.py:6326-6346`): `skip_thread` is computed there and the thread is
created immediately after. `DiscordAdapter`'s own body precedes
`ConciergeDiscordMixin` in the MRO (`adapter.py:1309`), so the mixin **cannot**
override that method — the adapter must DELEGATE, which is the established seam
convention in this package (`_concierge_free_response_channels`,
`runner._dynamic_runtime_profile`).

Add to `adapter.py`:

```python
def _wants_auto_thread(self, message, is_free_channel) -> bool:
    hook = getattr(self, "_concierge_wants_auto_thread", None)
    if callable(hook):
        try:
            if hook(message, is_free_channel):
                return True
        except Exception:  # fail closed to the base rule, never break intake
            ...
    return (not is_free_channel) or self._discord_free_response_auto_thread()
```

and let the existing gate call `self._wants_auto_thread(...)` instead of
computing `skip_thread` inline. `ConciergeDiscordMixin` supplies
`_concierge_wants_auto_thread`, returning True when the message's channel is in
the union above (home category, managed category, bound, or room).

Unchanged exclusions (still suppress threading): DMs, `discord.no_thread_channels`
(`_get_no_thread_channels`, `:5149`), `MessageType.reply` messages, and
voice-linked channels. Reuse `_auto_create_thread` (`:5610`) so the
`prospective_thread_id` stamping and semantic renaming behave identically to the
mention path.

### 5.3 The two defects this fixes

1. **Routing.** `_concierge_event_category_id` resolves no category for a thread,
   and discord.py's `Thread.category_id` **raises** `ClientException` when the
   parent is not cached (documented at `:880-891`). That exception lands in the
   wrap's fail-closed handler → the message delegates to the hub instead of the
   author's orchestrator. Resolving the category through `parent_id` makes
   threads behave like their parent channel — symmetric with the binding/room
   path, which already inherits the parent's registry key.
2. **Placement.** Worker channels were created in `self._concierge_event_category_id(event)`
   (`:1458`) — literally "wherever the user was typing". With a home category
   recorded, placement stops depending on the trigger event at all, so a dispatch
   triggered from a thread, another category, or voice all land in the same place.

## 6. Reconciliation of a deleted/missing category

`home_category` is a durable pointer that Discord can invalidate out from under
us (manual delete). Rules:

- A missing/unresolvable home category **must not** wedge routing: fall back to
  today's behavior (managed-category gate, else hub) and warn ONCE per user,
  keyed like the bootstrap's ghost/stat-error warnings (`_concierge_warn_once`,
  `:407`).
- It must also **not** trigger a silent re-creation of the category (an admin
  action), and must not bootstrap a duplicate orchestrator.
- `home_channel` liveness is already handled this way by the bootstrap's
  three-posture guard (`:962`); reuse the same posture vocabulary (live /
  ghost / stat_error / first_contact) rather than inventing a second one.

## 7. Worker placement during dispatch

`_concierge_dispatch_drive` (`:1412`) step 1 becomes:

```
home_category = registry.home_category_for_user(user_id)
category_id   = resolve_category(home_category) or self._concierge_event_category_id(event)
```

- Existing worker channels are still reused first (`channels_for_profile`,
  `:1453`) — a pre-home-category worker keeps its channel; only NEW channels are
  placed in the home category. This keeps the change non-destructive.
- Record the created channel's category in the binding if the binding schema
  carries it, so `/bot list` can group by category.

## 8. Config surface

`hermes_cli/config_defaults.py` (`concierge:` section, currently `:1515`),
with a runtime reader in `concierge/settings.py` (`ConciergeSettings`, `:14`)
— root invariant: every registered key needs a reader.

```yaml
concierge:
  home_category:
    enabled: false           # per-user home-category placement + scoping
    visibility: private      # /bot category default; the user picks
    general_channel: general
    unbound_scope: managed   # managed | guild
  threading:
    mode: mentions           # off | mentions | always
```

- `home_category.*` — where a user's spaces live and how far their orchestrator lane reaches.
- `threading.mode` — **surface-wide**, not home-category-only:
  - `off` — no auto-threads anywhere (even on @mention).
  - `mentions` — today's behavior: thread an @mention, reply inline in free-response channels.
  - `always` — thread every inbound message in a concierge-owned channel
    (home category, managed category, bound channel, or room).

Recommended production setting (this deployment): `home_category.enabled: true`,
`unbound_scope: guild`, `threading.mode: always`. Keep the DEFAULTS conservative so
a disabled/absent config is byte-for-byte today's behavior — this is a fork-level
feature, and flipping a default that every Discord install inherits would silently
change unrelated deployments.

Note there is already a core `discord.free_response_auto_thread` key
(`_discord_free_response_auto_thread`, `adapter.py:5059`) that
`threading.mode: always` effectively supersedes for concierge surfaces. Decide
whether `always` should mean "ignore the core key" (proposed: yes, concierge wins
on its own channels, and the core key keeps governing everything else) or
"logical-OR with it". Do NOT change the core default.

## 9. Blast radius

| Area | Change |
|---|---|
| `concierge/registry.py` | `home_category` in `_USER_MUTABLE_FIELDS`, `_normalize_user_entry`, `set_user`/`update_user`, `home_category_for_user`; `clear_orchestrator` keeps it |
| `concierge/settings.py` | `home_category` sub-settings on `ConciergeSettings` |
| `hermes_cli/config_defaults.py` | `concierge.home_category` keys |
| `concierge/provision.py` | optional `category_id` on `create_bot`; preserve `home_category` on orchestrator swap |
| `plugins/platforms/discord/concierge_mixin.py` | thread-aware category resolution + `_concierge_event_in_scope`; home-category placement in bootstrap + dispatch |
| `plugins/platforms/discord/concierge_mixin_admin.py`, `concierge_mixin_provisioning.py`, `concierge_mixin_room_commands.py` | topical siblings (mounted as `DiscordAdapter` bases, moved code keeps its cap): `/bot category` visibility flag + general channel + registry record, `/bot space`, `/bot new` provisioning, `/room *` handlers |
| `plugins/platforms/discord/adapter.py` | add `_wants_auto_thread(...)` and delegate the inline gate at `:6326-6346` to it |
| `plugins/platforms/discord/concierge_voice.py` | restamp: resolve scope for thread-voice sources via parent |
| `hermes_cli/concierge.py`, `hermes_cli/subcommands/concierge.py` | `--category`, `--public` |
| tests | registry, routing/threads, dispatch placement, threading gate, CLI |

`concierge/routing.py` stays **UNTOUCHED** (design invariant: the registry owns
the decision; `routing.py` is pure and already inherits the parent correctly).

## 10. Invariants to preserve

- Registry is the single routing source of truth — `home_category` lives there,
  never in `gateway.profile_routes`.
- Fail-closed on every branch: unresolvable → hub, never a wrong profile and
  never a dropped message.
- Zero behavior change when `home_category.enabled` is false (default).
- Anti-loop: bot-authored messages never re-home and never dispatch; dispatch
  stays depth 1.
- Per-user isolation: session keys include the author id; Honcho partitions by
  inbound user id; terminal cwd stays namespaced per user.
- Admin-only category creation; the hub `.env` allowlist still governs admission
  for every bot's channels.
- One registry read on the hot path.

## 11. Test plan

- **registry:** `home_category` round-trips; tolerant read of junk; preserved
  across orchestrator reprovision/adopt swaps; absent key normalizes to `None`.
- **routing:** a message in a *thread* under a managed category re-homes to the
  author's orchestrator; two authors in the same unbound channel route to
  different orchestrators; `ghost`/`stat_error` postures still skip bootstrap;
  `unbound_scope: managed` keeps today's behavior byte-for-byte.
- **dispatch:** a dispatch triggered from another category places a NEW worker
  channel in the home category; an existing worker channel is reused unchanged;
  a missing/invalid home category degrades to today's placement with one warning.
- **threading (surface-wide):** a free-response message auto-threads and the
  reply lands in the thread in each of — a bound bot channel, a room channel, a
  managed-category channel, a home category; `no_thread_channels` still
  suppresses; replies and DMs still do not thread; `threading.mode: mentions`
  keeps today's inline behavior; a thread-initiated message does not nest a
  second thread.
- **isolation:** two users in one shared home-category thread get separate
  sessions (requires `thread_sessions_per_user`, §12).
- **CLI:** `--category`/`--public`, and `--category` without `--user` is refused.
- **gate:** the full concierge suites stay green.

## 12. Decisions (resolved by the operator, 2026-11-14)

### 12.1 Per-user sessions inside threads — **no change; keep the default**

`build_session_key` computes, outside DMs,
`isolate_user = group_sessions_per_user and not (thread_id and not thread_sessions_per_user)`
(`gateway/session.py:721`). With the defaults (`group_sessions_per_user: True`,
`thread_sessions_per_user: False`) a thread key carries **no participant id**, so two
humans in one thread share one session with that bot.

**Correction to the draft's framing:** this is not the hole the draft implied. With
`threading.mode: always` every inbound message gets its OWN thread, so the general
channel never collides — A's message threads to T1 (A's orchestrator), B's to T2
(B's orchestrator). The only shared transcript left is *two people deliberately
posting in the same thread*, where sharing is arguably what a shared thread means.

The alternative I floated — "have the concierge lane force it" — is also the wrong
move on the merits, for a concrete reason: `build_session_key` reads
`group_sessions_per_user` / `thread_sessions_per_user` from the **adapter's**
`config.extra` (`base.py:2463`), not from the routed profile, and it is called from
every Discord ingress path. Scoping it to one lane means core/adapter surgery the
plugin rules forbid. So: leave core defaults alone; `thread_sessions_per_user` stays
available as a deployment-level switch if you later want per-user transcripts
*within* a shared thread (with the odd consequence that one bot would give two people
different answers in the same thread).

**On "both users interacting with the original author's bot":** no — profile routing
and session keys are separate things. Unbound lanes already route **per author**: each
user's message runs as *their own* orchestrator, so nobody touches another user's
orchestrator. The session-key question was only ever about who shares a *transcript*.
In a **bound** channel (a worker bot's channel) both users do drive the same bot
profile — that is existing binding routing, unchanged by this design — but they get
separate transcripts unless they are in the same thread.

### 12.2 Short, compact sessions — **confirmed as the intent**

Yes. With per-thread sessions the durable layer is Honcho (per inbound user id) and
the session is deliberately disposable. That is the design's own logic, not a
compromise: channel = index, thread = task, Honcho = what survives. Short compact
sessions are strictly better here — cheaper prefetch, less compression loss.

**One caveat to carry, not a blocker:** `MULTI_USER_PLAN.md` §3 Phase 3 records that
Honcho's per-call user/session partitioning is **unverified** (external catalog
plugin), and its stated fallback was "disable memory for shared bots rather than risk
cross-user recall." Confirming Honcho is really keyed per inbound user id belongs in
this build's test plan rather than being inherited as an assumption.

### 12.3 Existing users / orphaned channels — **leave them; wipe and start fresh**

No adopt-in-place path, no migration. The registry starts empty by design and bot
profiles are disposable (`concierge/AGENTS.md` §3), so a wipe is cheap.

### 12.4 Guild-wide scoping — **confirmed (`unbound_scope: guild`)**

Hub escape hatches that survive either way:
- **DMs** — no resolvable category → fail-closed → hub.
- **Admin slash commands** (`/bot`, `/room`, `/connect`) — separate `is_admin` path,
  unaffected by routing scope.
- **Ghost / stat_error orchestrators** — the three-posture guard degrades to the hub.
- **Rooms and bound channels** — still win over the orchestrator lane.

### 12.5 Naming — **private implied for one user; shared spaces named by topic**

- `/bot category <user>` → PRIVATE home category named `<display-name>`; the existing
  `-private` suffix is dropped.
- A public/shared space is not one user's category, so it is created with an
  operator-supplied **topic**. Proposed surface: `/bot space <name>`.

## 13. Follow-on (not blocking)

1. **Thread identity/reuse:** per-dispatch new thread (stateless, tidy) vs reusing a
   per-concept thread ("doctor's appointment"). Your forty-concepts example hints at
   the latter; per-dispatch is the simpler first cut and can be upgraded later.
2. **Worker dispatch threading** interacts with the reply-fetch window
   (`_concierge_fetch_room_history`, mixin `:1738`): once a worker's answer lands in a
   dispatch-created thread, the driver must read the thread's window, not the
   channel's.

