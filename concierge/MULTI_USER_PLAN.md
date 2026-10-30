# Multi-user Concierge — handoff planning doc

**Goal:** let more than one human use a single remote concierge deployment without one user's
sessions, memory, or data leaking into another's. Handoff target: a cloud/VM Hermes agent that
will implement and verify. Source of truth for current architecture: `concierge/AGENTS.md`,
`concierge/DISCORD_INTEGRATION.md`, `concierge/provision.py`, `concierge/routing.py`.

## 0. Current state (what already works for N users)

- Admission is already allowlist-based: `DISCORD_ALLOWED_USERS` / `DISCORD_ALLOWED_ROLES` in the
  **hub** profile's `.env` (authorization runs under the transport home, so one list governs
  every bot channel). Adding users today = edit that var + restart gateway.
- Per-channel bot routing already isolates *bots*: `resolve_target_profile` → `source.profile`,
  profile-namespaced session keys (`agent:<profile>:...`), and `_profile_runtime_scope` give each
  bot its own home, `.env`, memory store, cron store.
- What is **not** isolated is *users within a bot/channel*. A channel is one conversation
  surface shared by everyone admitted; the session key is channel-scoped, so User B can read or
  continue User A's transcript, and a bot's Honcho memory is one workspace per bot profile.

So the gap is **user-level isolation inside a bot**, plus admin gating, plus per-user data.

## 1. Privacy model we are building toward

Three guarantees, in priority order:

1. **Transcript isolation.** A user's turns with a bot are never visible to another user, even
   in the same channel. Enforced by session keys that include the Discord user id (and/or by
   per-user private channels/threads).
2. **Memory isolation.** Honcho (and any other memory provider) partitions by user id; a bot
   never recalls User A's preferences when answering User B.
3. **Data isolation.** Tools that touch disk/state (terminal, file, todo, cron) either run in a
   per-user sandbox/worktree, or are admin-only. A user cannot read another user's files or
   trigger another user's routines.

Non-goal for v1: per-user bot *profiles* (too heavy; one bot profile per channel stays the unit).

## 2. Design decisions (proposed)

### 2.1 Private spaces, not shared channels

- **Per-user personal categories** (the default private surface). When a user is admitted, the hub
  creates (or reuses) a Discord category named for that user, permission-locked so only that user
  and admins can see/create channels in it. The user provisions bots into channels inside their
  own category; they never need to talk to the concierge directly — their bots live there.
  Admins can still create public/shared spaces (categories/channels visible to multiple users).
- **No direct concierge chat.** Users interact with their bots in their private channels; the
  concierge hub is an admin/provisioning surface, not a conversational partner for end users.
- **Per-user threads** remain the opt-in "shared but private" surface inside a public channel.
- **Admin-only actions:** deleting another user's bot, creating public/shared spaces, retiring
  bots outside one's own category. Provisioning into one's own category is allowed for any
  admitted user.

- **Open follow-up:** an admin slash command (`/bot category <user>`) to create a
  permission-locked Discord category for a user is not yet implemented; categories are
  created manually until then.

- `build_session_key` / `_session_key_profile` path: when the source is a DM or a per-user
  thread, the key already differs by chat id. For shared channels where we still want isolation,
  append `user_id` to the key (e.g. `agent:<profile>:discord:<channel>:u:<user_id>`).
- Guard: never let two different `user_id`s resolve to the same session row. Add an invariant
  test in `tests/gateway/`.

### 2.3 Memory partitioning

- Honcho config carries `user_id` / session scoping. Provisioning must pass the inbound user id
  into the memory manager init, or key Honcho workspaces by `(bot_profile, user_id)`.
- The memory provider seam: `AIAgent(memory_manager=...)` and `MemoryManager` init. Ensure the
  user id flows from the inbound event through `_handle_message_with_agent` into the agent.
- Fallback: if a provider can't partition, disable memory for multi-user bots rather than mix.

### 2.4 Authorization tiers

- `DISCORD_ALLOWED_USERS` = full-trust (can provision bots, run admin commands, use
  terminal/file bots).
- New: `DISCORD_USER_USERS` (or a role) = chat-only (can talk to existing bots in their own
  private space, cannot provision, cannot use terminal/file toolsets).
- Admin commands (`/bot`, `/room`, `/connect`) check the caller is in the admin set; reject
  others with a short in-channel message.
- Toolset gating: bots that include `terminal`/`file` should be admin-only or run in a sandbox.

### 2.5 Data isolation for tool-using bots

- For a "developer" bot (repo work), run terminal commands in a per-user worktree/scratch dir,
  never the live checkout. The bot's `terminal.cwd` becomes `~/.hermes/workspaces/<user_id>/`
  (or a git worktree of the repo). This prevents cross-user file reads and protects the live
  gateway checkout.
- Todo/cron: per-user todo lists (todo toolset is already per-profile; for multi-user within a
  profile, key todos by user id) and per-user cron jobs (or disable cron for shared bots).

### 2.6 Cron routines in multi-user bots

- A routine that posts to a channel leaks to everyone. Options: (a) disable routines on
  multi-user/shared bots, (b) fan out one delivery per authorized user's private space, (c) only
  allow routines on single-owner bots.
- Recommend (c) for v1: routines are a single-owner feature; multi-user bots are on-demand.

## 3. Implementation plan (phased)

**Phase 1 — Admission + tiers (small)**
- Add `DISCORD_ADMIN_USERS` / `DISCORD_USER_USERS` (or role variants) to the env allowlist
  machinery; hub reads both.
- Gate `/bot`, `/room`, `/connect` on admin membership.
- Tests: `tests/gateway/test_discord_concierge_*` for admin rejection.

**Phase 2 — Session isolation (core)**
- Thread `user_id` through `SessionSource` → session key. Shared-channel keys include user id.
- Add DM routing: hub bot answers in DMs with isolated session keys.
- Invariant tests for no cross-user session reuse.

**Phase 3 — Memory partitioning** (status: fallback chosen)
- Honcho is an external provider not in this repo; per-call user/session partitioning is unverified
  (open question #1). Per the plan's fallback clause, multi-user/shared bots run with memory
  DISABLED rather than risk cross-user recall, until Honcho partitioning is confirmed.
- Single-owner bots keep memory enabled (backward compat).

**Phase 4 — Data isolation**
- Per-user `terminal.cwd` / worktree for terminal/file bots.
- Per-user todo scoping.
- Disable cron on multi-user bots (or fan-out later).

**Phase 5 — Developer bot blueprint (optional)**
- Add `developer` blueprint: toolsets `("file", "terminal")`, `core` preset, dev soul, runs in
  per-user worktree. Admin-only provisioning.

**Phase 6 — Verification**
- Two-user E2E: User A and User B DM the bot; assert transcripts and memory don't cross.
- Cross-user read attempt fails (file tool sandbox).
- Admin command rejected for non-admin.

## 4. Open questions for the implementer

1. Does Honcho's client accept a per-call user/session id, or does it need a separate workspace
   per user? Check `plugins/memory/honcho` (if present) / the external catalog plugin.
2. Discord DMs: does the bot need the `dm` scope / `MESSAGE_CONTENT`? Confirm the bot can
   receive and reply in DMs with current intents.
3. Per-user worktrees: git worktree per user on the VM may be heavy; consider a single shared
   read-only checkout + per-user scratch for edits, with PRs as the deliverable.
4. Should multi-user be opt-in per deployment (config flag `concierge.multi_user`) to keep the
   single-owner default simple? Recommend yes.

## 5. Risks

- Session-key changes can orphan existing sessions (migration needed: old keys become
  "legacy" and are read-only).
- Memory partitioning requires provider support; verify before committing.
- Per-user worktrees multiply disk usage on the VM.

## 6. Acceptance criteria (done when)

- Two distinct Discord users can both use the concierge (DMs at minimum).
- Neither can read the other's transcript or memory.
- Non-admins cannot provision bots or run terminal/file bots.
- Terminal/file bots operate in per-user sandboxes.
- Existing single-user deployments keep working unchanged (backward compat).
- Tests cover the isolation invariants.
