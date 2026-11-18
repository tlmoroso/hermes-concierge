# Voice home-session re-key

**Operator decision (2026-11-17, confirmed):** re-key the concierge
speaker→orchestrator voice lane to the SPEAKER'S HOME-CHANNEL session.
Spoken turns continue the same conversation as typed turns in the user's
home `general` channel. Per-speaker by construction (the session key
carries the user id), so a shared voice channel works multi-user. No
memory provider needed.

## The bug

The right bot ran the spoken turn, on the wrong SESSION. Flow (verified):
`_handle_voice_channel_input` (run_voice.py:305) builds the source with
`chat_id` = the voice-BOUND TEXT channel → `_restate_voice_routing_stamps`
(concierge_voice.py) → the lane re-homed only `source.profile` to the
speaker's orchestrator → the runner canonicalizes AFTER the hook
(run_voice.py:331), so the session key derived
`agent:<orchestrator>:discord:group:<BOUND-CHANNEL>:<speaker>` — a fresh
conversation with zero shared context with the speaker's home channel
(`agent:<orchestrator>:discord:group:<HOME>:<speaker>`, what typed turns
in home derive). Empirical probe on base: identity
`runtime_profile=orchestrator`, key `…:group:700:42` for a speaker whose
home is `9001`.

## The fix — re-key BEFORE the verdict pins anything

`_concierge_voice_orchestrator_lane` (concierge_voice.py:360) still
consumes only the verdict tail's `_CONCIERGE_ORCHESTRATOR_ATTR` stamp
(placed exactly when NO binding/room verdict claimed the bound channel
AND the speaker has a LIVE orchestrator; bound and room channels keep
their routing untouched). With the stamp in hand:

1. **Gate** (`_concierge_voice_home_rekey_target`, concierge_voice.py:428):
   the home id is the OWNING user's RECORDED one — `registry.get_user`
   (the canonical users-section accessor; tolerant of hand-edited files;
   it re-reads the registry once per SPOKEN utterance, not per message) —
   never derived from the trigger event. Shape checks ride the ONE
   registry read the verdict already paid: the home channel must be
   BOUND to exactly the stamped orchestrator (provisioning's
   `bind_channel`; concierge/home_space.py:137) and be no room, and the
   recorded guild — when present — must be the voice guild.
2. **Re-key** (`_concierge_voice_rekey_to_home`, concierge_voice.py:477):
   `chat_id = home`, `chat_type = "group"`, `thread_id = None`,
   `parent_chat_id = None` (the session-bearing shape a typed message in
   home carries — `build_session_key` reads exactly these;
   gateway/session.py:691); `user_id` stays the SPEAKER (per-user
   isolation preserved). `chat_name`/`chat_topic` follow (the session
   prompt renders them, session.py:404-413 — a stale bound-channel label
   would flip the home session's prompt on every modality switch);
   resolution tries the receiving bot's client, then the orchestrator
   profile's bot (which IS in the private home channel), else None.
3. **Admission reconciles BY CONSTRUCTION — now via branch (a):** the
   lane re-runs `_concierge_apply_registry_verdict` on the re-keyed
   source, so it carries the IDENTICAL stamps a fresh `build_source` for
   the home channel gets — profile re-homed via the home BINDING,
   connect-nudge stamp included, orchestrator stamp CLEARED — and
   `_concierge_dynamic_runtime_profile` (concierge_mixin.py:2755)
   confirms via branch (a) (the home binding), not branch (c). A
   `profile_mismatch` drop is impossible: the gate verified the exact
   shape admission confirms, against the same registry read.

Probe on the branch: re-keyed source → identity
`runtime_profile=orchestrator`, `runtime_home=<orchestrator profile>`,
session key `agent:<orch>:discord:group:<HOME>:<speaker>` — byte-equal to
a typed turn in home.

## Blast radius

- **Playback stays in the VOICE channel.** The finding the task asked to
  verify was HALF right: the runner fallback `_deliver_voice_reply`
  (run_voice.py:466) is guild-keyed (`raw_message.guild_id`), but the
  path that actually plays spoken turns is the BASE auto-TTS chain
  (base.py:4505 → `_wants_auto_tts` gate at 4124 → `_play_tts_file` →
  `play_tts`), and BOTH of its chat_id reads pointed at the bound channel
  before the re-key. Three seams now resolve the VOICE GUILD through the
  home→guild lane map (`_voice_home_lanes`, registered by the re-key,
  pruned to live voice calls):
  - `_should_auto_tts_for_chat` override (concierge_voice.py:608): the
    gate MIRRORS the bound channel's verdict, so `/voice on|tts|off`
    keep owning spoken turns; only VOICE-type events reach auto-TTS, so
    typed replies never start speaking.
  - `play_tts` (adapter_voice_info.py:46, MOVED from adapter.py:3722 —
    the file-size ratchet; adapter.py only shrinks): home chat id → lane
    guild → `play_in_voice_channel`, else the send_voice fallback.
  - `_voice_ack_guild_for` (run_voice.py:211): the verbal-ack guild via
    the lane map, still mixer-gated.
- **Text replies land in the HOME channel** (the point of the re-key):
  base delivery sends to `event.source.chat_id`.
- **The transcript ECHO still goes to the BOUND text channel**
  (run_voice.py:355-361, keyed on `text_ch_id` directly) and the
  duplicate-transcript binding keeps `(profile, guild, BOUND channel)`
  (run_voice.py:350 — `text_ch_id`, never the re-keyed id).
- **Per-channel prompt/skills now resolve from the post-hook
  `source.chat_id`** (run_voice.py:373-385): byte-identical when no hook
  re-keyed; the HOME channel's context when it did — the same context a
  typed turn in that session carries (prompt-cache invariant: no
  per-modality prompt flips inside one session).
- **Unchanged:** binding/room verdicts (the stamp never lands there), the
  bootstrap lane (no stamp → no lane), bot-authored utterances (anti-loop
  guard kept), the spoken-summary relay, `/voice` command semantics.

## Fail-closed (today's behaviour stands)

No home recorded / home unbound / home bound to a different profile /
home is a room / recorded guild ≠ voice guild / registry unreadable /
no voice guild on the source / any fault in the lane → the stamp's plain
re-home on the bound-channel session, exactly today. The lane NEVER
raises (intake never breaks).

## The typed lanes stay alone (decision)

`_concierge_inbound_dispatch` branch (c) (concierge_mixin.py:726+) was
considered and NOT re-keyed, three reasons:

1. **The typed turn never lands on an orchestrator-namespaced
   unbound-channel session to re-key.** Empirical probe (real
   `handle_message` → wrap → delegation): the adapter pins the turn's
   `RoutingIdentity` at ingress (base.py:3942-3951) BEFORE the wrap's
   re-home runs (base.py:4482 → wrap), so a typed managed-category
   message from an orchestrator user derives
   `agent:main:discord:group:<channel>:<user>` with
   `runtime_profile='default'` — the typed re-home changes
   `source.profile` for delivery, not the pinned identity. There is no
   "typed orchestrator session keyed to the unbound channel" whose
   stranding this fix could share; a typed re-key would mean moving the
   re-home into `build_source` (pre-pin) — a different, larger change to
   typed ingress order than this task.
2. **Typed replies must stay in the channel the user typed in.** Voice
   has a separate playback channel (the VC) and the operator explicitly
   decided text replies to spoken turns land in home; typed input has no
   such separation — re-keying would move the answer away from where the
   question was asked.
3. **The typed lane is the bootstrap surface** (first contact in managed
   categories provisions the orchestrator) — its session semantics are
   load-bearing for provisioning, out of scope here.

## Tests

`tests/gateway/test_discord_concierge_voice_orchestrator.py` (extended —
this file is owned by this change): spoken utterance lands on the home
session key; per-speaker isolation (two speakers, two home sessions);
identical-stamps proof vs fresh `build_source` for home; binding/room
keep routing with a recorded home; no-orchestrator/no-home/guild-mismatch
fail-closed rows; playback gate + `play_tts` + ack guild still resolve
the VOICE channel; echo still the bound channel; prompt/skills from the
post-hook source.
