"""Concierge mixin for the Discord adapter: per-channel profile routing (Task
5.3) + the ``/bot`` command surface, managed-channel auto-offer, and
hub-side channel creation (Tasks 5.4/5.5/5.7).

Where this sits and why (verified by the Task 5.1 spike, DISCORD_INTEGRATION.md):
``BasePlatformAdapter.build_source`` is the single ingress chokepoint where
per-channel profile routing already happens — ``gateway.profile_routes`` stamps
``source.profile`` there, and every downstream consumer (session key, identity,
resolution, ``_profile_runtime_scope``) reads what it stamps. The concierge rides
the SAME seam: resolve the channel→bot binding from ``concierge/registry.json``
(the runtime-mutable source of truth — a ``/bot new`` on a phone takes effect on
the next message, no restart) with the pure Task 5.2 ``resolve_target_profile``,
and stamp the verdict on ``source.profile`` BEFORE ``build_source`` returns.

NO second turn-execution mechanism is added: the existing
``_make_default_profile_message_handler`` already scopes every turn to the
identity's ``runtime_home`` (run_adapters.py:1412–1424), session keys are
profile-namespaced (base.py:2360) and authorization stays transport-scoped
(run.py:4035 — the hub's allowlist governs admission for every bot's channels).
Wrapping the turn a second way would fight all three.

Gates, in order:
1. ``concierge.enabled`` false (or no section) → ZERO behavior change: no
   registry read, no state, base ``build_source`` output byte-identical. The
   verdict comes from the IN-HAND ``gateway_runner.config`` (the concierge
   section rides the same config.yaml→GatewayConfig bridge as every other
   gateway key) — never a config-file read on the message path.
2. ``multiplex_profiles`` false → skip routing (same gate as
   ``GatewayRunner._profile_name_for_source``: the scoped run only activates
   under multiplexing, so a stamp here would profile-namespace session keys
   while the agent still ran in ``agent:main`` — routing inert AND lying in
   session metadata).
3. A ``source.profile`` stamped with a profile OTHER than the adapter's
   owner profile (an explicit ``gateway.profile_routes`` match) → config.yaml
   is authoritative over the runtime registry; the registry only fills in.
   A stamp EQUAL to the owner profile is the base's DEFAULT (it stamps
   ``... or owner_profile`` whenever no route matched), not a decision: the
   registry verdict still applies (the 2026-09-27 live bug was gate 3
   reading that default as decided and never consulting the registry).
4. A ``profile_route_rejected`` source → the fail-closed drop stands; the
   registry never resurrects a rejected route.

Fail-closed parity with config routes: a binding whose profile no longer exists
(bot retired under us, hand-edited registry) degrades to the hub with ONE
WARNING per (channel, profile) pair — never re-homes to a ghost, never to
``agent:main``, and never floods the log at one WARNING per message. An
unreadable registry file is skipped the same way: the hub answers, once per
channel.

The command/event surface (Tasks 5.4/5.5/5.7) rides the same gates at
REGISTRATION time (``_concierge_events_wanted``): disabled means no ``/bot``
group on the tree, no ``on_guild_channel_create`` handler, and the
``on_thread_create`` chain passes straight through to the existing normalizer.
Payloads are built by the PURE module ``concierge/discord_payloads.py``
(blueprint picker, list rows, retire choices, create-vs-bind plan) — unit-tested
without discord.py; only the discord.py plumbing is deferred to the Task 5.6
manual E2E. The slow service calls (``provision.create_bot`` /
``provision.retire_bot``, and the ``bot_status`` cron-store walk behind
``/bot list``) run via ``asyncio.to_thread`` — they are filesystem + cron
work under a cross-process flock that can wait up to 60s and must never
freeze the gateway's event loop — and every handler that does slow work
answers with the adapter's own sequencing: cheap auth gate, ``defer()``,
work, reply through the followup (an unanswered interaction token burns
after ~3s, taking even the failure copy with it). Event chaining follows
the spike's rule (discord.py allows ONE
handler per event name, so a second ``@client.event`` would REPLACE the
adapter's): the adapter's registration block routes ``on_thread_create``
through ``_concierge_on_thread_create``, which runs the concierge auto-offer
FIRST (never blocking the normalizer on it) and then calls the existing
``_on_platform_thread_create``.

ROOMS (Task 7.3) split the M5 mechanism in two, at the same seams:

- DECISION, at ``build_source`` (the M5b seam, unchanged): the room lookup
  runs FIRST — call order per the plan, a channel is a room XOR a binding —
  via the pure Task 7.2 ``resolve_room_targets``. A room channel's source
  carries the ordered member list as ``_concierge_room_targets`` (a
  process-local, wire-invisible marker in the ``profile_route_rejected``
  spirit) and NO single profile: the re-home stamp is the one-bot mechanism,
  a room is N turns. Ghost members are skipped with the same
  warn-once/fail-closed posture as ghost bindings.
- EXECUTION, at the RUNNER'S MESSAGE-HANDLER SEAM (``set_message_handler``
  installs a concierge wrap as ``_message_handler`` — the ONE attribute
  ``_process_message_background`` awaits, so every live dispatch funnels
  through it whatever entry computed the session key; the 2026-10-29 live
  bug was the fan-out living in a ``handle_message`` override upstream's
  refactor had left off that chain): an event whose source carries targets
  is NEVER dispatched as itself; a fan-out DRIVER task runs the members
  instead, spawned off the inbound path so the discord.py loop is never
  blocked (the same returns-quickly-spawns-task contract the base's own
  dispatch already has). Per member, IN STORED ORDER: a fresh source stamped
  ``profile = member`` (the M5b stamping mechanism, per member —
  ``dataclasses.replace`` gives each member its own source so the cached
  routing identity is resolved per member, not reused from the previous
  one) and a fresh event, dispatched through the REAL
  ``super().handle_message`` — canonicalization, profile-namespaced session
  keys, busy-guard install and (through ``_process_message_background`` →
  the wrap → the stored runner handler) the runner's
  ``_profile_runtime_scope`` plus the adapter-side response delivery all
  run exactly as for a single-bot message; NO second turn mechanism. The
  driver then waits for that member's session task to finish before
  dispatching the next member (SEQUENTIAL turns, desktop-room parity: member
  N+1's turn starts only after member N's reply landed), bounded by a
  generous circuit breaker that gives up WAITING (never cancels the turn —
  ``asyncio.shield``) and continues the fan-out. The wrap is FAIL-CLOSED:
  any concierge-branch error warns once and delegates to the stored runner
  handler — message intake never breaks.
- Mention addressing, v1: NONE. All room members share ONE Discord identity
  (the hub's bot), so a ``<@BOT_ID>`` token names the shared identity, not a
  member — no per-member Discord identity exists to address. The desktop
  room precedent has no cheap mechanism to copy either
  (``tui_gateway/hosted_room_driver.py`` carries no mention addressing; the
  spike note's M7 recommendation is per-member events + channel history,
  which is what this implements). ``mentioned_profiles`` therefore stays an
  empty tuple at this seam: no mention and @shared-bot mention are BOTH
  broadcasts to all members. The pure layer's mention path
  (``resolve_room_targets(mentioned_profiles=...)``) stays tested for when a
  per-member addressing scheme lands.
- History: a room WINDOW, not the standard backfill (M7 review fix). The
  adapter's ``channel_context`` backfill (``_fetch_channel_context``)
  SELF-PARTITIONS at the shared bot identity's last message — correct for a
  single-bot channel (the bot's own session carries its prior turns), wrong
  for a room: under the concierge's ONE Discord identity every member's
  reply IS ``self``, so the partition would hide every sibling member's
  reply and the bots would answer the user independently instead of
  coordinating, which is the whole point of a room. The fan-out driver
  therefore fetches its own window ONCE per inbound room message
  (``_concierge_fetch_room_history``: all conversational authors INCLUDING
  the shared identity, windowed to desktop-room parity — newest ~200
  messages, ~32k chars, truncated from the oldest end) and stamps it as
  each member turn's ``channel_context`` — the same field the standard
  backfill populates and ``run_inbound`` prepends ahead of ``[New
  message]``. A failed fetch logs ONE warning and members fall back to
  whatever context the standard path produced; the single-bot path's
  self-partition itself is untouched. The window's LINES go through the
  adapter's ONE canonical formatter (``_format_channel_history_line``, the
  same function ``_fetch_channel_context`` formats through), so the two
  windows' injection contracts — [unverified] tagging + preamble,
  DISCORD_ALLOW_BOTS, the ``[bot]`` suffix — cannot drift apart; only the
  keep-vs-partition policy differs (the room never breaks at the shared
  identity).
- Error isolation (the plan's requirement): a member whose dispatch or turn
  task ERRORS gets ONE "⚠ <bot> is unavailable" post into the channel and
  the remaining members still run. Turn-body errors are already contained by
  the standard machinery (``_process_message_background`` logs and posts its
  own error reply); the notice covers the dispatch/await failure paths.

ACCOUNT CONNECTIONS (Task 8.2) — the ``/connect`` command and the
first-message prerequisite nudge — ride the same two seams and the same
gates:

- DECISION, at ``build_source`` (where the binding is already in hand from
  the routing read — no second registry read): a BOUND bot whose blueprint
  declares ``connects`` the binding has not been nudged about gets the
  unnudged, UNCONFIGURED kinds (``concierge/connections.account_probe`` —
  CHEAP-LOCAL reads only: stat/which/env, verified against the real check
  implementations; the google-workspace ``setup.py --check`` does a network
  OAuth refresh and is deliberately NOT the message-path probe) stamped on
  the source as a wire-invisible marker, exactly like the room targets.
- EXECUTION, at the wrap: the FIRST inbound message in a bound
  channel posts ONE nudge ("I need access to your Calendar… tap here to
  connect: <link>") into the channel BEFORE the bot's turn dispatches.
  Nudge state is REGISTRY-PERSISTED on the binding
  (``nudged_connections``, registry.mark_connections_nudged) — the chosen
  home because it survives gateway restarts (an in-memory set would
  re-nudge the user on every boot) and dies with the binding it belongs to.
  The flag is marked only AFTER a delivered nudge (a failed post is not
  burned) and the whole nudge path is error-isolated: a nudge failure NEVER
  breaks the turn. Rooms are out of scope for v1 (documented: no single
  binding to mark).
- ``/connect [kind]`` registers alongside /bot and /room with the same
  self-gating: no kind = this guild's needed account kinds with
  configured/unconfigured status + a deep-link BUTTON per unconfigured
  kind; a kind = the ephemeral deep link for that kind. Links are built by
  the PURE ``concierge/connections.py`` (paths discovered in Task 8.1,
  base URL = the dashboard's own ``public_url`` config) — no new OAuth
  plumbing, only deep links into the dashboard's existing pages.

MEMORY REPORT (Task 9.2) — ``/bot memory`` rides the same seams: the
``memory`` subcommand sits in the /bot group (same self-gating, disabled
concierge registers nothing), the handler defers BEFORE the work (client
construction + the dialectic query are network I/O to the HUB's Honcho)
and runs the whole build OFF the loop via ``asyncio.to_thread`` — the
``/bot list`` contract. The report itself is the PURE-ish
``concierge/memory_report.py``: one dedicated Honcho session, the plugin's
own prewarm question asked about the user peer with ``raise_errors=True``
(a dead backend is an error, not an empty answer), peer-card facts and
saved conclusions as bullet sections, bounded short. Failure copy on ANY
error — construction, query, auth — never a traceback; the stubbed-client
unit tests live in tests/concierge/test_memory_report.py.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

# Wire-invisible, process-local marker: the ordered room members a source's
# event must fan out to (applied by the registry-verdict tail,
# ``ConciergeVoiceMixin._concierge_apply_registry_verdict`` in the
# concierge_voice sibling — reached from BOTH build_source and the
# voice-ingress restamp hook — and consumed by the set_message_handler
# wrap). Never serialized (SessionSource.to_dict reads declared fields
# only).
_ROOM_TARGETS_ATTR = "_concierge_room_targets"

# Wire-invisible, process-local marker (orchestrator redesign, design §4):
# the author's OWN orchestrator profile for a message in an UNBOUND channel —
# stamped by the registry-verdict tail (the concierge_voice sibling, only
# after NO binding/room verdict matched, so the stamp implies "unbound" by
# construction) and consumed by the set_message_handler wrap, which re-homes
# ``source.profile`` to it before delegation when the channel sits in a
# managed category. Admission confirms via the users section (branch (c) of
# ``_concierge_dynamic_runtime_profile``). Same discipline as
# _ROOM_TARGETS_ATTR: never serialized.
_CONCIERGE_ORCHESTRATOR_ATTR = "_concierge_orchestrator_profile"

# Wire-invisible, process-local marker for the Task 8.2 connection nudge:
# ``(registry_binding_key, missing_kind, ...)`` — the binding the nudge must
# mark and the UNCONFIGURED account kinds the bound bot needs and has not been
# nudged about (stamped by build_source, consumed by the
# set_message_handler wrap). Same discipline as _ROOM_TARGETS_ATTR: never
# serialized.
_CONCIERGE_CONNECT_NUDGE_ATTR = "_concierge_connect_nudge"

# Voice-relay stamp (Task 3): the member profile a fan-out lane turn runs
# for — set by _concierge_run_member_turn on the member event AFTER the
# dataclasses.replace clones (plain attrs do not survive a replace), read
# by the auto-TTS seam overrides in ConciergeVoiceMixin to decide whether
# the lane's answer is spoken verbatim or as an auxiliary-LLM summary
# (concierge.voice_relay). Same discipline as _ROOM_TARGETS_ATTR:
# wire-invisible, process-local, never serialized.
_VOICE_RELAY_ATTR = "_concierge_voice_relay_member"

# Wire-invisible, process-local marker (orchestrator redesign, PLAN decision
# 2): stamped on the PHASE-B continuation event the dispatch driver clones,
# consumed by the wrap's dispatch guard — a continuation turn NEVER
# re-dispatches (depth 1), whatever the orchestrator answers with. Stamped
# AFTER the dataclasses.replace clones (plain attrs do not survive a
# replace). Same discipline as _ROOM_TARGETS_ATTR: never serialized.
_CONCIERGE_DISPATCH_ATTR = "_concierge_dispatch_continuation"

# The orchestrator's dispatch directive (PLAN decision 2): when work belongs
# to a worker blueprint the ENTIRE response is exactly
# ``@dispatch <blueprint-key> <task text>`` — the key is a CATALOG worker
# blueprint (never ``orchestrator`` itself), and the task is everything after
# it. Matched against the response STRIPPED (tolerating surrounding
# whitespace); DOTALL lets the task run past the first line while the
# response must still OPEN with the directive.
_DISPATCH_DIRECTIVE_RE = re.compile(r"^@dispatch ([a-z_]+) (.+)$", re.DOTALL)

# One history window line authored by the shared bot identity — the shape
# ``_format_channel_history_line`` formats (``[Name [bot]] content``; foreign
# bots are dropped by the formatter under the default DISCORD_ALLOW_BOTS, so
# a ``[bot]``-suffixed line IS the shared identity's). The dispatch driver's
# reply parser takes the NEWEST such line as the OPENING of the worker's
# reply MESSAGE — everything after it up to the next author line below is
# that same reply, so a multi-paragraph answer is captured WHOLE.
_WORKER_REPLY_LINE_RE = re.compile(r"^\[.*\[bot\]\] (.+)$")

# A history window line that OPENS a message — any of the formatter's author
# shapes (``[name] ``, ``[name [bot]] ``, ``[unverified] [name] ``): the
# dispatch reply parser's message boundary. A multi-line message's
# continuation lines are emitted RAW by the formatter, so they do not match;
# the accepted ambiguity is a reply paragraph that itself opens with
# ``[x] `` (a line-based window cannot tell it from a new author).
_WORKER_REPLY_AUTHOR_LINE_RE = re.compile(r"^\[.*\] ")

# Circuit breaker on waiting for ONE member's turn: generous vs a normal
# agent turn (minutes), bounded vs a wedged one (which would silently stop
# the room's remaining members from ever answering). Giving up waits ONLY —
# the member's turn itself is shielded, never cancelled.
_ROOM_MEMBER_TURN_WAIT_SECONDS = 600.0

# Hub-created channels suppress the create-side auto-offer for a short TTL:
# the hub's own channel creations (``/room create``, ``/bot new``'s create
# plan) post their own greeting/confirmation into the new channel, so the
# ``on_guild_channel_create`` bot picker would be noise on top of it (the
# 2026-10-29 live wart: a picker landed in a fresh room channel 0.4s before
# the room registered). Short TTL so a stale entry is harmless — Discord
# never reuses a channel id, but a wedged entry must not silence the offer
# for a genuinely fresh channel forever.
_HUB_CREATED_CHANNEL_TTL_SECONDS = 30.0

# Desktop-parity window for the room fan-out's shared channel history (the
# M7 review gap): the newest ~200 messages, ~32k characters total, truncated
# from the OLDEST end. Mirrors the hosted-room driver's transcript window so
# a Discord room member sees roughly what a desktop room member sees.
_ROOM_HISTORY_MESSAGE_CAP = 200
_ROOM_HISTORY_CHAR_CAP = 32_000

# Module-level once-latch for the settings-read warning: a broken reader is a
# process-lifetime state (fix = repair the install, restart), so one WARNING
# says everything a per-message WARNING would — minus the spam.
_settings_read_warned = False


def _concierge_enabled_from_config(runner) -> bool:
    """``concierge.enabled`` from the runner's IN-HAND ``GatewayConfig`` — the
    section rides the config.yaml→GatewayConfig bridge (gateway/config_loader),
    so this is a dataclass attribute read, not a config-file load. The verdict
    flips on gateway restart (or a re-loaded config), the same lifecycle as
    ``multiplex_profiles`` itself; a runtime flip of ``concierge.enabled`` is a
    restart-level change, not a per-message one. Fail-closed to disabled: a
    missing/None config or a reader failure never breaks message intake — the
    reader blow-up warns ONCE per process (a warning per message would just be
    log spam for a state that only a restart fixes)."""
    try:
        from concierge.settings import concierge_settings

        config = getattr(runner, "config", None)
        section = getattr(config, "concierge", None)
        # ``{"concierge": section}`` — the reader merges over DEFAULT_CONFIG,
        # so a missing section still yields the registered default (enabled
        # False) rather than an honest-looking lie.
        return concierge_settings({"concierge": section} if isinstance(section, dict)
                                  else None).enabled
    except Exception:
        global _settings_read_warned
        if not _settings_read_warned:
            _settings_read_warned = True
            logger.warning("concierge: could not read concierge settings from the "
                          "gateway config; per-channel routing disabled",
                          exc_info=True)
        return False


# The dispatch driver's fetch anchor (PLAN decision 2): the worker lane is
# SYNTHETIC — no Discord message exists for it — but the reply fetch rides
# ``_concierge_fetch_room_history``, which reads ``raw_message.channel`` and
# anchors ``before=raw_message``. This duck-typed stand-in carries the WORKER
# channel object plus a message id minted ahead in time, so the worker's
# reply (posted by the normal delivery machinery, seconds ago) is INSIDE the
# window instead of past its anchor. Plain class, not a discord.py type: the
# base turn chain only ever duck-types the raw message (reply anchoring
# reads the event's own ``message_id``), and the forum-seed precedent already
# passes a channel object as a lane's raw message.
_DISPATCH_FETCH_ANCHOR_AHEAD_SECONDS = 3600.0


class _DispatchFetchAnchor:
    """Duck-typed raw-message stand-in for a synthetic dispatch lane."""

    __slots__ = ("channel", "id")

    def __init__(self, channel, message_id: int) -> None:
        self.channel = channel
        self.id = message_id


def _dispatch_fetch_anchor_id() -> int:
    """A Discord snowflake minted ``_DISPATCH_FETCH_ANCHOR_AHEAD_SECONDS``
    into the future — any message posted while this driver runs is strictly
    OLDER than the anchor, so the fetch window sees the worker's reply."""
    return int(((time.time() + _DISPATCH_FETCH_ANCHOR_AHEAD_SECONDS) * 1000.0
                - 1420070400000.0) * (1 << 22))


def _concierge_channel_slug(raw: str) -> str:
    """Discord-safe slug of a display name/user id for channel names:
    lowercase ascii alphanumerics with single hyphens. Never raises; never
    empty (``user`` fallback). Shared by the orchestrator and worker channel
    name helpers so the two cannot drift."""
    slug = "".join(ch if ch.isascii() and ch.isalnum() else "-"
                   for ch in raw.lower())
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "user"


class ConciergeDiscordMixin:
    """``build_source`` override carrying the concierge routing verdict.

    Mounted ahead of ``BasePlatformAdapter`` in the Discord adapter's MRO
    (``class DiscordAdapter(DiscordAuthorizationMixin, DiscordMediaMixin,
    ConciergeDiscordMixin, ConciergeVoiceMixin, …, BasePlatformAdapter)``)
    so the override applies to EVERY ingress path that builds a source —
    plain messages, slash commands, platform events — with zero changes at
    the call sites. NOT self-contained: it MOUNTS TOGETHER with
    ``ConciergeVoiceMixin`` (the concierge_voice sibling, which owns the
    registry-verdict machinery) and reaches the shared
    helpers — ``_concierge_registry_or_none``,
    ``_concierge_apply_registry_verdict`` — through ``self``, which the
    MRO resolves onto the companion mixin at call time (companion methods
    are declared here only as ``Any``-typed names). ``build_source`` and
    ``_concierge_route_for_source`` therefore REQUIRE that companion in
    the bases; the pair composes with any base providing
    ``build_source``.

    Warning dedup: the fail-closed WARNINGs (ghost binding, unreadable
    registry) fire ONCE per distinct key, on a class-level set — a busy
    retired-bot channel must not add one WARNING line per message to
    gateway.log. Class-level, not instance, because a re-connected adapter
    is the same incident, not a fresh one; the set is bounded by the
    registry's own binding count PLUS one registry-unreadable key per
    channel (that incident is keyed by channel id, not by a binding — the
    binding count alone does not bound it).
    """

    _concierge_warned: set[tuple[str, ...]] = set()

    def _concierge_warn_once(self, key: tuple[str, ...], message: str,
                             *args: Any, exc_info: bool = False) -> None:
        """Emit ``message % args`` as a WARNING the first time ``key`` is seen;
        silent after. No TTL: every key names a registry state (a binding, a
        channel) whose fix (re-provision, unbind, repair the file) clears the
        log noise by construction — the set is bounded by the registry, and a
        warning that re-fires after the operator fixed the state would be a
        lie."""
        if key in self._concierge_warned:
            return
        self._concierge_warned.add(key)
        logger.warning(message, *args, exc_info=exc_info)

    def build_source(self, *args: Any, **kwargs: Any):
        # Gate 1 — the zero-behavior-change gate FIRST: disabled means no
        # concierge work at all; the base implementation is byte-for-byte what
        # a concierge-less build runs. Reads the in-hand runner config: no
        # config-file I/O on the message path, disabled or not.
        runner = getattr(self, "gateway_runner", None)
        if not _concierge_enabled_from_config(runner):
            return super().build_source(*args, **kwargs)
        # Gate 2 — multiplexing gate (mirrors _profile_name_for_source): the
        # profile-scoped run only activates under multiplexing.
        if not getattr(getattr(runner, "config", None), "multiplex_profiles", False):
            return super().build_source(*args, **kwargs)

        # Gates passed: make sure the runner can RESOLVE the registry stamps
        # this path is about to produce (admission's last-resort resolver;
        # idempotent, one identity compare when already wired). The connect
        # path also wires it — this covers a runner attached after connect.
        self._concierge_wire_dynamic_router(runner)

        # The config seam runs first (inside base build_source); the registry
        # only fills in when the config seam made NO decision.
        source = super().build_source(*args, **kwargs)
        # Gate 3 — an explicit config route already decided. The base stamps
        # ``source.profile`` with the adapter's OWNER profile whenever no
        # ``gateway.profile_routes`` entry matched (``... or owner_profile``),
        # so a stamp EQUAL to ``_owner_profile`` is the DEFAULT, not a
        # decision: under a multiplex hub with no routes it is set on every
        # source, and reading it as "decided" would short-circuit the registry
        # lookup entirely — the live 2026-09-27 bug (a bot-bound channel
        # answered by the hub because gate 3 mistook the default stamp for a
        # config verdict). Only a stamp that DIFFERS from the owner profile is
        # a real ``profile_routes`` match and stays authoritative over the
        # runtime registry.
        stamped_profile = getattr(source, "profile", None)
        if stamped_profile and stamped_profile != getattr(self, "_owner_profile", None):
            return source
        # Gate 4 — a rejected route's fail-closed drop stands.
        if getattr(source, "profile_route_rejected", False) is True:
            return source

        # ONE registry read feeds both verdicts (the registry read is the
        # accepted M5b per-message fast path — one json load per enabled
        # message, not one per section). None = unreadable; degrade to the
        # hub with the warn-once posture below.
        reg = self._concierge_registry_or_none(source)
        if reg is not None:
            self._concierge_apply_registry_verdict(source, reg)
        return source

    # ── Task 8.2: account-connection nudges (decision at build_source) ─────

    def _concierge_binding_key(self, source, bindings) -> str | None:
        """The registry key the routing verdict's binding lives under — the
        channel itself, else its parent (threads/forum posts inherit the
        parent's binding), in the same lookup order
        ``resolve_target_profile`` applied."""
        for candidate in (getattr(source, "chat_id", None),
                          getattr(source, "parent_chat_id", None)):
            key = str(candidate) if candidate is not None else ""
            if key and key in bindings:
                return key
        return None

    def _concierge_stamp_connect_nudge(self, source, reg,
                                       profile: str) -> None:
        """Stamp the connection-nudge intent on *source* when the bound
        bot's blueprint needs an account kind that is UNCONFIGURED and not
        yet nudged about on this binding.

        Costs, in order: connectless blueprint (the common case) stops at a
        tuple read; an already-nudged binding stops at a list check; only
        then does the probe run — CHEAP-LOCAL reads (stat/which/env; see
        concierge/connections.py for why the network-capable checks were
        rejected). A decision failure never breaks routing: the worst case
        is no nudge.
        """
        try:
            from concierge import connections
            from concierge.blueprints import CATALOG

            bindings = reg.get("bindings") or {}
            key = self._concierge_binding_key(source, bindings)
            if key is None:
                return
            binding = bindings.get(key) or {}
            if not isinstance(binding, dict):
                return
            blueprint = CATALOG.get(str(binding.get("blueprint") or ""))
            if blueprint is None:
                return
            connects = [k for k in (getattr(blueprint, "connects", ())
                                     or ()) if isinstance(k, str)]
            if not connects:
                return
            stored = binding.get("nudged_connections")
            nudged = ([k for k in stored if isinstance(k, str)]
                      if isinstance(stored, list) else [])
            candidates = [k for k in dict.fromkeys(connects)
                          if k not in nudged]
            if not candidates:
                return
            missing = connections.missing_connections(
                candidates, connections.account_probe(profile))
            if missing:
                setattr(source, _CONCIERGE_CONNECT_NUDGE_ATTR,
                        (key, *missing))
                logger.debug(
                    "[%s] Concierge: channel %s (profile %s) missing "
                    "account kinds %s — nudge pending",
                    getattr(self, "name", "discord"), source.chat_id,
                    profile, missing)
        except Exception:
            logger.debug(
                "[%s] concierge: connection-nudge decision failed; "
                "message routing proceeds without a nudge",
                getattr(self, "name", "discord"), exc_info=True)

    # ── Task 7.3/8.2 EXECUTION: the runner's message-handler seam ──────────
    #
    # The 2026-10-29 live bug: upstream's inbound refactor moved the live
    # chain to ``_start_session_processing`` → ``_process_message_background``
    # → ``self._message_handler(event)`` — the RUNNER's handler installed via
    # ``set_message_handler`` — and this mixin's ``handle_message`` override
    # (where the fan-out + nudges used to live) fell out of that chain: dead
    # code on the live path, so a room message ran the hub lane instead. The
    # consumption now rides the seam every live dispatch funnels through,
    # whatever entry computed the session key: the wrap below.

    def _concierge_room_targets_on(self, event) -> tuple[str, ...]:
        """The fan-out targets stamped on *event*'s source by ``build_source``
        (empty when the event is not a room fan-out — the common case, and
        the pure pass-through path)."""
        source = getattr(event, "source", None)
        targets = getattr(source, _ROOM_TARGETS_ATTR, None) if source is not None else None
        if isinstance(targets, tuple) and targets:
            return targets
        return ()

    def set_message_handler(self, handler) -> None:
        """Install *handler* under a CONCIERGE WRAP (the runner's own seam).

        The wrap (``_concierge_inbound_dispatch``) consumes the room-targets
        stamp (fan-out driver, never delegated) and posts the Task 8.2
        connect nudge before delegating; everything else delegates 1:1. The
        wrap is installed UNCONDITIONALLY — it is a pure pass-through for
        events without concierge stamps, and those stamps only exist when
        concierge routing ran (``build_source`` gates), so a non-concierge
        install sees no stamps and behaves exactly like the base. Re-sets
        re-wrap around the STORED runner handler, never the wrap itself
        (idempotent); ``None`` restores the base's exact clear semantics.
        """
        if handler is None:
            self._concierge_runner_message_handler = None
            super().set_message_handler(None)
            return
        installed = getattr(self, "_concierge_inbound_wrap", None)
        if (installed is not None
                and getattr(self, "_message_handler", None) is installed):
            # Already wrapped: swap the stored runner handler underneath the
            # single installed wrap — never double-wrap.
            self._concierge_runner_message_handler = handler
            return
        self._concierge_runner_message_handler = handler

        async def _concierge_inbound_wrap(event):
            return await self._concierge_inbound_dispatch(event)

        super().set_message_handler(_concierge_inbound_wrap)
        self._concierge_inbound_wrap = _concierge_inbound_wrap

    async def _concierge_inbound_dispatch(self, event):
        """The wrap body: ONE place consumes the concierge stamps.

        (a) room-targets stamp → spawn the fan-out DRIVER task (exactly the
            old ``handle_message`` contract: returns quickly, the driver
            runs the members) and RETURN — a room message is fully consumed
            by the fan-out, that is the designed semantics;
        (b) connect-nudge stamp → post the nudge FIRST, then delegate;
        (c) orchestrator stamp (design §4) → re-home ``source.profile`` to
            the author's orchestrator before delegation (the channel is
            unbound by construction of the stamp; admission confirms via
            the users section), else — for a first contact with NO
            orchestrator entry at all — spawn the background bootstrap (a
            dead or uncheckable orchestrator skips it with one warning
            per user), never blocking the message;
        (d) otherwise delegate 1:1 to the stored runner handler.
        FAIL-CLOSED: any error in a concierge branch warns once and the
        message is delegated to the runner handler — intake never breaks.
        """
        runner_handler = getattr(self, "_concierge_runner_message_handler", None)
        try:
            targets = self._concierge_room_targets_on(event)
        except Exception as e:
            self._concierge_warn_once(
                ("wrap-room-stamp",),
                "[%s] concierge: reading the room-target stamp failed (%s); "
                "the message is delegated to the normal lane instead",
                getattr(self, "name", "discord"), e, exc_info=True)
            targets = ()
        if targets:
            task = None
            try:
                channel_id = str(
                    getattr(getattr(event, "source", None), "chat_id", "") or "")
                task = asyncio.create_task(
                    self._concierge_run_room_turns(event, targets))
                self._concierge_track_fanout_task(channel_id, task)
                logger.debug(
                    "[%s] concierge: room fan-out started for channel %s "
                    "(%d members)",
                    getattr(self, "name", "discord"), channel_id, len(targets))
                return None
            except Exception as e:
                self._concierge_warn_once(
                    ("wrap-fanout",),
                    "[%s] concierge: spawning the room fan-out failed (%s); "
                    "the message is delegated to the normal lane instead",
                    getattr(self, "name", "discord"), e, exc_info=True)
                if task is not None:
                    # Never run the message twice: the half-spawned driver is
                    # cancelled before the fallback turn dispatches.
                    task.cancel()
        try:
            nudge = self._concierge_connect_nudge_on(event)
        except Exception:
            nudge = None
        if nudge is not None:
            try:
                await self._concierge_post_connect_nudge(event, nudge)
            except Exception as e:
                logger.warning(
                    "[%s] concierge: connection nudge failed for channel %s; "
                    "the turn proceeds normally: %s",
                    getattr(self, "name", "discord"), nudge[0], e)
        # Orchestrator redesign (design §4), the unbound lanes. Order matters:
        # room stamp → nudge → HERE → delegate, so neither branch disturbs
        # the fan-out or nudge contracts. Both are fail-closed to today's hub
        # behavior; neither ever blocks or breaks the delegation below.
        orchestrator = None
        try:
            orchestrator = self._concierge_orchestrator_fallback_on(event)
        except Exception as e:
            self._concierge_warn_once(
                ("wrap-orchestrator",),
                "[%s] concierge: the orchestrator fallback decision failed "
                "(%s); the message is delegated to the normal lane instead",
                getattr(self, "name", "discord"), e, exc_info=True)
        if orchestrator is not None:
            try:
                event.source.profile = orchestrator
                logger.debug(
                    "[%s] concierge: unbound channel %s re-homed to the "
                    "author's orchestrator %s",
                    getattr(self, "name", "discord"),
                    getattr(getattr(event, "source", None), "chat_id", "?"),
                    orchestrator)
            except Exception as e:
                self._concierge_warn_once(
                    ("wrap-orchestrator-rehome",
                     str(getattr(getattr(event, "source", None), "chat_id", "?"))),
                    "[%s] concierge: re-homing channel %s to the orchestrator "
                    "failed (%s); the hub answers instead",
                    getattr(self, "name", "discord"),
                    getattr(getattr(event, "source", None), "chat_id", "?"), e)
        else:
            # Maybe spawn the bootstrap (never raises, never blocks — the
            # task runs in the background and the message delegates
            # normally, hub lane, exactly as before). First CONTACT only:
            # a dead or uncheckable orchestrator entry skips it with one
            # warning per user — never provision over the fault.
            self._concierge_maybe_bootstrap_orchestrator(event)
        if runner_handler is None:
            # The base's no-handler contract, kept exact under the wrap: an
            # adapter that still polls and sends must not silently pretend.
            if not getattr(self, "_concierge_no_runner_handler_logged", False):
                self._concierge_no_runner_handler_logged = True
                logger.error(
                    "[%s] Dropping inbound message: no gateway message "
                    "handler is installed on this adapter. The adapter is "
                    "connected and can send, but every inbound message is "
                    "discarded.", getattr(self, "name", "discord"))
            return None
        response = await runner_handler(event)
        # Orchestrator dispatch (PLAN decision 2): a REGISTRY-CONFIRMED
        # orchestrator turn that answered with the ``@dispatch`` directive
        # hands the exchange to the background driver — the directive
        # response itself is SUPPRESSED (None: the driver delivers
        # everything). Every guard failure keeps the response UNCHANGED
        # (fail-closed: the orchestrator answered alone).
        dispatch = self._concierge_dispatch_directive_on(event, response)
        if dispatch is None:
            return response
        blueprint_key, task = dispatch
        channel_id = str(
            getattr(getattr(event, "source", None), "chat_id", "") or "")
        driver = None
        try:
            driver = asyncio.create_task(
                self._concierge_orchestrator_dispatch(
                    event, task, blueprint_key))
            self._concierge_track_fanout_task(channel_id, driver)
        except Exception as e:
            # Never run the message twice: the half-spawned driver is
            # cancelled before the fallback answer is returned (the room
            # fan-out's spawn-failure contract).
            if driver is not None:
                driver.cancel()
            self._concierge_warn_once(
                ("dispatch-spawn", channel_id),
                "[%s] concierge: spawning the orchestrator dispatch driver "
                "failed for channel %s (%s); the orchestrator's answer is "
                "delivered as-is instead",
                getattr(self, "name", "discord"), channel_id, e,
                exc_info=True)
            return response
        logger.info(
            "[%s] concierge: orchestrator dispatch started for channel %s "
            "(blueprint %s)",
            getattr(self, "name", "discord"), channel_id, blueprint_key)
        return None

    # ── Orchestrator dispatch (PLAN decision 2): the wrap capability ──────

    def _concierge_dispatch_directive_on(self, event, response):
        """The dispatch verdict for *event*'s turn response:
        ``(blueprint_key, task)`` when the response is the orchestrator's
        ``@dispatch`` directive AND every guard holds, else ``None``.

        Guards (ALL must hold, fail-closed to "no dispatch"):
        - the event is human-authored (``source.is_bot`` False — a bot's
          own directive-shaped answer never dispatches; anti-loop);
        - the event does NOT carry the process-local dispatch-continuation
          stamp (a phase-B turn never re-dispatches — depth 1);
        - the response is a non-empty plain string whose (stripped) first
          line is the directive, with a CATALOG worker blueprint key —
          never ``orchestrator`` (no self-dispatch); every other response
          shape (media objects, ephemeral wrappers, None) falls through;
        - the turn ran AS the author's registry orchestrator
          (``source.profile == orchestrator_for_user(user_id)``,
          anti-spoof) — ONE registry read, paid ONLY on a
          directive-shaped response (the wrap runs on every message; the
          common case must stay read-free), tolerant → no dispatch.
        """
        source = getattr(event, "source", None)
        if source is None or getattr(source, "is_bot", False):
            return None
        if getattr(event, _CONCIERGE_DISPATCH_ATTR, None):
            return None
        if not isinstance(response, str):
            return None
        match = _DISPATCH_DIRECTIVE_RE.match(response.strip())
        if match is None:
            return None
        blueprint_key, task = match.group(1), match.group(2).strip()
        if not task or blueprint_key == "orchestrator":
            return None
        try:
            from concierge.blueprints import CATALOG

            if blueprint_key not in CATALOG:
                return None
        except Exception as e:
            self._concierge_warn_once(
                ("dispatch-catalog",),
                "[%s] concierge: the blueprint catalog could not be read "
                "for the dispatch decision (%s); the response is delivered "
                "as-is instead",
                getattr(self, "name", "discord"), e, exc_info=True)
            return None
        user_id = str(getattr(source, "user_id", "") or "")
        if not user_id:
            return None
        try:
            from concierge import registry

            orchestrator = registry.orchestrator_for_user(user_id)
        except Exception as e:
            self._concierge_warn_once(
                ("dispatch-registry", user_id),
                "[%s] concierge: the orchestrator lookup for user %s failed "
                "(%s); the response is delivered as-is instead",
                getattr(self, "name", "discord"), user_id, e, exc_info=True)
            return None
        if not orchestrator or source.profile != orchestrator:
            return None
        return blueprint_key, task

    # ── Task 8.2: account-connection nudges (execution at the wrap) ────────

    def _concierge_connect_nudge_on(self, event) -> tuple | None:
        """The nudge stamp ``build_source`` left on *event*'s source:
        ``(registry_binding_key, missing_kind, ...)``, or ``None`` for the
        common case (unbound channel, connectless blueprint, all
        configured, already nudged, or a room)."""
        source = getattr(event, "source", None)
        stamp = (getattr(source, _CONCIERGE_CONNECT_NUDGE_ATTR, None)
                 if source is not None else None)
        if isinstance(stamp, tuple) and len(stamp) >= 2:
            return stamp
        return None

    async def _concierge_post_connect_nudge(self, event, stamp) -> None:
        """Post the ONE prerequisite nudge into the channel, then mark the
        binding's once-flag — REGISTRY-persisted (``nudged_connections``)
        so a gateway restart never re-nudges the user. Send FIRST, mark
        AFTER: a post that cannot be delivered does not burn the once-flag
        (the next message retries); the residual race — two first messages
        nudging twice — is self-limiting and harmless. Fully
        error-isolated: a nudge failure NEVER breaks the turn.

        The registry write is flock-guarded and synchronous, so it runs on
        a worker thread (``asyncio.to_thread``, the M5c discipline); the
        rare concurrency race between two first messages' marks is safe by
        construction (``mark_connections_nudged`` MERGES).
        """
        binding_key, kinds = stamp[0], [str(k) for k in stamp[1:]]
        try:
            from concierge import connections, registry

            links = connections.connect_links(
                kinds, connections.dashboard_base_url())
            text = connections.build_nudge_response(kinds, links).get("text")
            if not text:
                return
            channel = getattr(
                getattr(event, "raw_message", None), "channel", None)
            if channel is None or not hasattr(channel, "send"):
                logger.warning(
                    "[%s] concierge: no channel to post the connection "
                    "nudge for channel %s; the binding stays unmarked and "
                    "the next message retries",
                    getattr(self, "name", "discord"), binding_key)
                return
            await channel.send(text)
            await asyncio.to_thread(
                registry.mark_connections_nudged, binding_key, kinds)
            logger.info(
                "[%s] concierge: posted the account-connection nudge for "
                "channel %s (kinds %s)",
                getattr(self, "name", "discord"), binding_key, kinds)
        except Exception as e:
            logger.warning(
                "[%s] concierge: connection nudge failed for channel %s; "
                "the turn proceeds normally: %s",
                getattr(self, "name", "discord"), binding_key, e)

    # ── Orchestrator redesign (design §4): unbound fallback + bootstrap ────
    #
    # Two unbound-channel lanes at the wrap, both fail-closed to today's hub
    # behavior. The STAMP lives on the source (registry-verdict tail, the
    # concierge_voice sibling — only set when NO binding/room verdict
    # matched); the CATEGORY lives on the raw channel (the event), which is
    # why the re-home happens HERE and not at the verdict tail.

    def _concierge_event_category_id(self, event) -> str | None:
        """The trigger channel's category id (normalized str) — duck-typed off
        the RAW message's channel, exactly the way the event-surface helpers
        read it (``category_id``, else ``category.id``). ``None`` when the
        event carries no channel or no resolvable category, which every
        consumer reads as a fail-closed "not managed".

        Threads and forum posts: the category is resolved from the RAW
        channel object only — this helper walks no parent chain of its
        own. For a thread the only candidate view is discord.py's own
        ``Thread.category_id`` property, which resolves the PARENT's
        category when the parent is cached and RAISES ClientException
        when it is not (the duck-typed read does not swallow that — the
        consumers' fail-closed handlers do). The unbound fallback lanes
        (re-home + bootstrap) are therefore NOT guaranteed inside
        threads/forum posts — fail-closed, and asymmetric with the
        binding/room path BY DESIGN (those inherit the parent channel's
        registry key, because the registry owns that decision). Do not
        "fix" this by walking the parent without revisiting the
        bootstrap's assumptions (the personal channel is created in the
        TRIGGER channel's category, which a thread does not have)."""
        channel = getattr(getattr(event, "raw_message", None), "channel", None)
        if channel is None:
            return None
        raw = getattr(channel, "category_id", None)
        if raw is None:
            raw = getattr(getattr(channel, "category", None), "id", None)
        if raw is None:
            return None
        return str(raw)

    def _concierge_event_in_managed_category(self, event) -> bool:
        """True when the trigger channel sits in one of the configured managed
        categories (the same list the auto-offer picker gates on). Anything
        unresolvable — no category on the channel, unreadable settings —
        resolves False: the unbound→orchestrator lanes must fail closed to
        today's hub behavior, never re-home or bootstrap on a guess."""
        settings = self._concierge_settings_obj()
        if settings is None:
            return False
        category_id = self._concierge_event_category_id(event)
        if category_id is None:
            return False
        managed = {str(c) for c in (settings.managed_category_ids or ())}
        return category_id in managed

    def _concierge_orchestrator_fallback_on(self, event) -> str | None:
        """The unbound→orchestrator re-home verdict (design §4) for *event*:
        the orchestrator profile to re-home to, or ``None`` (today's hub
        behavior, unchanged). Requires ALL of a human-authored event
        (anti-loop: a bot's own message never re-homes to the orchestrator),
        the ``_CONCIERGE_ORCHESTRATOR_ATTR`` stamp the registry-verdict tail
        left on the source (only ever set for an UNBOUND channel, by
        construction), and a trigger channel inside a managed category —
        the category check needs the RAW channel, which lives on the event,
        not the source, and is why the re-home happens at the wrap instead
        of in the verdict tail. The stamp is a plain str the tail validated
        against the live registry (liveness included), moments ago on this
        same dispatch; admission re-confirms via branch (c)."""
        source = getattr(event, "source", None)
        if source is None or getattr(source, "is_bot", False):
            return None
        stamp = getattr(source, _CONCIERGE_ORCHESTRATOR_ATTR, None)
        if not isinstance(stamp, str) or not stamp:
            return None
        if not self._concierge_event_in_managed_category(event):
            return None
        return stamp

    def _concierge_source_unbound(self, source, reg) -> bool:
        """True when the source's channel — or its parent, threads/forum
        posts inherit (``_concierge_binding_key``'s resolution) — is NEITHER
        a single-bot binding NOR a room: the fallback stamp's
        by-construction precondition, re-checked here so a registry write
        that raced the stamp still fails closed."""
        bindings = reg.get("bindings")
        if isinstance(bindings, dict):
            if self._concierge_binding_key(source, bindings) is not None:
                return False
        rooms = reg.get("rooms")
        if isinstance(rooms, dict):
            for candidate in (getattr(source, "chat_id", None),
                              getattr(source, "parent_chat_id", None)):
                cid = str(candidate) if candidate is not None else ""
                if cid and isinstance(rooms.get(cid), dict):
                    return False
        return True

    def _concierge_user_orchestrator(self, users,
                                     user_id: str) -> tuple[str | None, str]:
        """The author's orchestrator POSTURE from the registry's users
        section: ``(profile, posture)``, the postures the bootstrap lanes
        distinguish explicitly (a dead or uncheckable orchestrator is NOT
        first contact):

        - ``"first_contact"`` — no users entry, or an entry with no
          orchestrator field: the ONLY posture that bootstraps (design §4).
        - ``"live"`` — an entry whose profile dir exists: route (the
          orchestrator-stamp lane owns it), never bootstrap.
        - ``"ghost"`` — an entry whose profile dir is MISSING: SKIP the
          bootstrap — ``provision.create_bot`` is user-keyed idempotent and
          returns the dead entry WITHOUT rebuilding the dir, so a bootstrap
          here would create + bind a fresh home channel onto the dead
          profile (a permanently wedged channel; the stamp tail already
          warned, so there is no route either). The caller warns once per
          user: re-provision or retire.
        - ``"stat_error"`` — the liveness check itself failed (EACCES,
          EIO, a stalled mount): SKIP, never provision over an
          environmental fault. The caller warns once per user: investigate
          the filesystem.

        ``users`` is the users section of the ONE registry read the caller
        already paid (None / non-dict → first contact) — this helper does
        NO registry I/O of its own, keeping the hot path's one-read budget.
        Never raises: an unclassifiable failure resolves to ``stat_error``
        (fail-closed — never bootstrap over a fault we could not name).
        """
        try:
            entry = users.get(str(user_id)) if isinstance(users, dict) else None
            if not isinstance(entry, dict):
                return None, "first_contact"
            profile = entry.get("orchestrator")
            if not isinstance(profile, str) or not profile.strip():
                return None, "first_contact"
            from plugins.platforms.discord.concierge_voice import _profile_exists

            exists, stat_error = _profile_exists(profile)
            if stat_error is not None:
                return profile, "stat_error"
            if not exists:
                return profile, "ghost"
            return profile, "live"
        except Exception:
            return None, "stat_error"

    def _concierge_skip_bootstrap_warn(self, user_id: str, profile,
                                       posture: str) -> None:
        """The warn-once for a bootstrap skipped on a dead or uncheckable
        orchestrator entry — SHARED by the decision and the in-task
        re-check so the two call sites cannot drift, and keyed per USER so
        a wedged entry warns once however many messages hit it (the fix —
        re-provision, retire, or repair the filesystem — clears the noise
        by construction). ``live`` is silent: routing, not an incident."""
        if posture == "ghost":
            self._concierge_warn_once(
                ("bootstrap-ghost", user_id),
                "[%s] concierge: user %s has a registry orchestrator (%r) "
                "whose profile directory is missing (retired under us?); "
                "skipping the first-contact bootstrap — re-provision the "
                "orchestrator or retire the users entry",
                getattr(self, "name", "discord"), user_id, profile)
        elif posture == "stat_error":
            self._concierge_warn_once(
                ("bootstrap-stat", user_id),
                "[%s] concierge: the orchestrator for user %s (profile %r) "
                "could not be checked; skipping the first-contact "
                "bootstrap — investigate the profile directory/filesystem "
                "before re-provisioning",
                getattr(self, "name", "discord"), user_id, profile)

    def _concierge_maybe_bootstrap_orchestrator(self, event) -> None:
        """First-contact bootstrap DECISION (design §4): a human-authored,
        managed-category message in an UNBOUND channel from an author with
        NO orchestrator AT ALL spawns ONE background bootstrap task
        (``_concierge_orchestrator_bootstrap``), tracked like a fan-out
        driver per channel id. Never raises and never blocks: the
        triggering message delegates as the hub either way, and the NEXT
        message routes to the orchestrator. Single-flight per USER via a
        lazy in-flight set (check-then-add with no await between is atomic
        on the event loop), so a burst of first messages spawns ONE
        bootstrap; the task itself re-checks the users section and exits
        unless the posture is still first contact (the second backstop).

        The author's registry posture (``_concierge_user_orchestrator``)
        decides — a dead or uncheckable orchestrator is NOT first contact:
        ``first_contact`` bootstraps; ``live`` skips silently (the stamp
        lane owns routing); ``ghost`` and ``stat_error`` skip with ONE
        warning per user (a ghost must not bootstrap — the idempotent
        provision would hand back the dead entry and the bootstrap would
        bind a fresh home channel onto the dead profile; an uncheckable
        orchestrator must never be provisioned over an environmental
        fault).

        HOT-PATH BUDGET: the registry read is for genuinely-unbound sources
        ONLY. A room-targets stamp, or a binding/config verdict already on
        the source — ``source.profile`` set to something OTHER than the
        adapter's owner default, exactly build_source gate 3's distinction
        between a REAL config route and the base's default stamp, and the
        shape ``_concierge_apply_registry_verdict`` leaves on a bound
        source — means the channel is not unbound: skip BEFORE any registry
        work, so every BOUND-channel message in a managed category (the
        normal concierge layout) and every member-lane room turn keeps the
        ONE registry read for the whole routing decision. A message that
        raced a concurrent bind/room write simply skips (fail-closed)."""
        try:
            source = getattr(event, "source", None)
            if source is None or getattr(source, "is_bot", False):
                return
            if getattr(source, _CONCIERGE_ORCHESTRATOR_ATTR, None):
                return  # a stamp means an orchestrator exists: not first contact
            if getattr(source, _ROOM_TARGETS_ATTR, None):
                # A room message (or a member-lane clone carrying the
                # stamp): the fan-out owns it — not a first contact, and no
                # registry read to find that out.
                return
            stamped_profile = getattr(source, "profile", None)
            if (stamped_profile is not None
                    and stamped_profile != getattr(self, "_owner_profile", None)):
                # A binding verdict re-homed the source (the
                # registry-verdict tail stamps ``source.profile`` = the
                # bound bot) or a real config route matched (build_source
                # gate 3: only a stamp DIFFERING from the owner default is a
                # decision) — the channel is not unbound, skip before any
                # registry work.
                return
            if not self._concierge_event_in_managed_category(event):
                return
            user_id = str(getattr(source, "user_id", "") or "")
            if not user_id:
                return
            from concierge import registry

            # ONE registry read for the unbound check + the users-section
            # posture (tolerate failure → skip: a bootstrap must never run
            # on an unreadable registry); the posture reuses THIS read,
            # never a separate users lookup.
            reg = registry.load_registry()
            if not self._concierge_source_unbound(source, reg):
                return  # raced a concurrent bind/room write: fail closed
            profile, posture = self._concierge_user_orchestrator(
                reg.get("users"), user_id)
            if posture != "first_contact":
                # live: the stamp lane owns routing. ghost / stat_error:
                # never bootstrap over a dead or uncheckable orchestrator.
                self._concierge_skip_bootstrap_warn(user_id, profile, posture)
                return
            inflight = getattr(self, "_concierge_bootstrap_inflight", None)
            if inflight is None:
                inflight = self._concierge_bootstrap_inflight = set()
            if user_id in inflight:
                return
            inflight.add(user_id)
            try:
                task = asyncio.create_task(
                    self._concierge_orchestrator_bootstrap(event))
            except Exception as e:
                inflight.discard(user_id)
                self._concierge_warn_once(
                    ("bootstrap-spawn", user_id),
                    "[%s] concierge: spawning the orchestrator bootstrap "
                    "failed for user %s (%s); the next message retries",
                    getattr(self, "name", "discord"), user_id, e,
                    exc_info=True)
                return
            self._concierge_track_fanout_task(
                str(getattr(source, "chat_id", "") or "?"), task)
            logger.info(
                "[%s] concierge: first contact from user %s in channel %s — "
                "orchestrator bootstrap started",
                getattr(self, "name", "discord"), user_id,
                getattr(source, "chat_id", "?"))
        except Exception as e:
            self._concierge_warn_once(
                ("bootstrap-decision",),
                "[%s] concierge: the orchestrator bootstrap decision failed "
                "(%s); the message is delegated to the normal lane instead",
                getattr(self, "name", "discord"), e, exc_info=True)

    async def _concierge_orchestrator_bootstrap(self, event) -> None:
        """The first-contact bootstrap driver (design §4): provision the
        author's orchestrator, create their personal channel in the trigger
        channel's category, record the home + binding in the registry,
        suppress the create event's auto-offer, and post the greeting + a
        one-line pointer in the trigger channel.

        FAIL-CLOSED: every step rides one try — any error warns ONCE (keyed
        per user) and the bootstrap aborts; the triggering message is
        UNAFFECTED (it already delegated to the hub — this task runs AFTER
        delegation was initiated, never blocking it). A later message
        retries only while the author still has NO orchestrator entry —
        once the users entry lands, the re-check below exits, so the retry
        window is exactly the window before a successful (or external)
        provision. Provision/registry work is filesystem + flock work under
        ``asyncio.to_thread`` (the M5c discipline of
        ``_concierge_post_connect_nudge``). The in-flight set entry is
        released in ``finally`` either way."""
        user_id = str(getattr(getattr(event, "source", None), "user_id", "")
                      or "")
        try:
            # Single-flight backstop: a bootstrap that lost a race re-checks
            # the users section first and exits unless the posture is STILL
            # first contact — the winning contact's bootstrap already
            # happened (live), or the entry died or became uncheckable
            # meanwhile (ghost / stat_error: never provision over those,
            # the same three-posture rule the decision applies; the SHARED
            # warn keys mean a state both seams saw warns once, not twice).
            # An unreadable registry here also exits: the load raises into
            # the fail-closed handler below — never provision over an
            # environmental fault.
            if not user_id:
                return
            from concierge import registry

            reg = registry.load_registry()
            profile, posture = self._concierge_user_orchestrator(
                reg.get("users"), user_id)
            if posture != "first_contact":
                self._concierge_skip_bootstrap_warn(user_id, profile, posture)
                return
            source = getattr(event, "source", None)
            guild_id = str(getattr(source, "guild_id", "") or "") or None
            category_id = self._concierge_event_category_id(event)
            from concierge import provision, registry

            # 1) provision the author's orchestrator — user-keyed,
            #    channel-less, idempotent (may return already_provisioned:
            #    a race with another contact is fine, the entry is the truth).
            record = await asyncio.to_thread(
                provision.create_bot, "orchestrator",
                user_id=user_id, guild_id=guild_id, channel_id=None)
            profile = (record.get("orchestrator")
                       if isinstance(record, dict) else None)
            if not profile:
                raise RuntimeError(
                    "provisioning returned no orchestrator profile")
            # 2) the personal channel, in the trigger channel's category.
            home_channel = await self._concierge_create_channel(
                guild_id=guild_id, category_id=category_id,
                name=self._concierge_orchestrator_channel_name(source))
            if not home_channel:
                raise RuntimeError("the personal channel could not be created")
            # 3) registry: the home channel + the binding (binding LAST among
            #    the registry writes — until it lands, nothing routes there).
            await asyncio.to_thread(
                registry.update_user, user_id, home_channel=home_channel)
            await asyncio.to_thread(
                registry.bind_channel, home_channel, profile=profile,
                blueprint="orchestrator", guild_id=guild_id)
            # 4) the channel-create event's auto-offer picker must not fire
            #    on the hub's own channel (``_concierge_create_channel``
            #    already marked it; re-asserted here so the suppression
            #    cannot depend on the create helper's internals).
            self._concierge_remember_hub_created_channel(home_channel)
            # 5) greet in the new home + point at it from the trigger channel.
            title = self._concierge_member_title(profile)
            await self._concierge_greet_created_channel(home_channel, title)
            trigger = getattr(
                getattr(event, "raw_message", None), "channel", None)
            await self._concierge_post_to_channel(
                str(getattr(source, "chat_id", "") or ""),
                f"→ I set up a private channel for us: <#{home_channel}>",
                channel=trigger)
            # 6) the blueprint's routine, if it ships one (the orchestrator
            #    ships routine=None today — guarded so a future routine lands
            #    correctly with no further wiring). Mirrors provision.py's
            #    routine step: the ORCHESTRATOR's own cron store, origin =
            #    the new home channel.
            await asyncio.to_thread(
                self._concierge_attach_orchestrator_routine,
                profile, home_channel)
            logger.info(
                "[%s] concierge: orchestrator bootstrap for user %s complete "
                "(profile %s, home channel %s)",
                getattr(self, "name", "discord"), user_id, profile,
                home_channel)
        except asyncio.CancelledError:
            raise  # shutdown: cancel cleanly, no warn-once burn
        except Exception as e:
            self._concierge_warn_once(
                ("bootstrap", user_id),
                "[%s] concierge: the orchestrator bootstrap for user %s "
                "failed (%s); the triggering message was unaffected — a "
                "later message retries only while the author still has no "
                "orchestrator entry (once the users entry lands, the "
                "in-task re-check exits)",
                getattr(self, "name", "discord"), user_id, e, exc_info=True)
        finally:
            inflight = getattr(self, "_concierge_bootstrap_inflight", None)
            if inflight is not None:
                inflight.discard(user_id)

    @staticmethod
    def _concierge_orchestrator_channel_name(source) -> str:
        """Discord-safe name for the author's personal channel:
        ``orchestrator-<slug of the display name>`` (falling back to the user
        id), capped at Discord's 100-character limit. Never raises."""
        try:
            raw = str(getattr(source, "user_name", None)
                      or getattr(source, "user_id", None) or "user")
            return f"orchestrator-{_concierge_channel_slug(raw)}"[:100]
        except Exception:
            return "orchestrator-user"

    @staticmethod
    def _concierge_worker_channel_name(blueprint_key: str, source) -> str:
        """Discord-safe name for the lazily created worker channel:
        ``<blueprint key>-<slug of the display name>`` (the per-user suffix
        keeps two users' workers of the same blueprint distinct), capped at
        Discord's 100-character limit. Never raises."""
        try:
            raw = str(getattr(source, "user_name", None)
                      or getattr(source, "user_id", None) or "user")
            return f"{blueprint_key}-{_concierge_channel_slug(raw)}"[:100]
        except Exception:
            return f"{blueprint_key}-user"

    def _concierge_attach_orchestrator_routine(self, profile: str,
                                               home_channel: str) -> None:
        """Attach the orchestrator blueprint's routine (provision.py's routine
        step, mirrored) in the ORCHESTRATOR's OWN cron store
        (``use_cron_store``), origin = the new home channel. Guarded on
        ``bp.routine`` — the orchestrator ships routine=None today, so this
        is inert until a future routine lands; errors propagate to the
        bootstrap's fail-closed handler (profile, channel and binding are
        already committed by then)."""
        # DRIFT RISK: this INLINE call duplicates provision.py's
        # routine-creation step (``create_bot``'s ``if bp.routine ...``
        # block — the ``create_job`` call under ``use_cron_store(<profile
        # dir>)``), deliberately NOT shared in this fix (scope). When a
        # real orchestrator routine lands, keep the two ``create_job``
        # calls field-for-field identical or refactor to one shared
        # helper: a divergence means a channel-bound provision and a
        # bootstrap provision ship different routines.
        from concierge.blueprints import MODEL_PRESETS, get_blueprint
        from concierge.provider_groups import (
            apply_group_to_preset as _apply_group)
        from cron.jobs import create_job, use_cron_store
        from hermes_cli.profiles import get_profile_dir

        bp = get_blueprint("orchestrator")
        if not bp.routine:
            return
        preset = MODEL_PRESETS[bp.model_preset]
        model = _apply_group(None, preset)
        with use_cron_store(get_profile_dir(profile)):
            create_job(
                prompt=bp.routine.prompt,
                schedule=bp.routine.schedule,
                name=f"[bot:{profile}] {bp.routine.name}",
                model=model.model,
                provider=model.provider,
                enabled_toolsets=list(bp.toolsets),
                monitor_script=bp.routine.monitor,  # None = always run
                deliver="origin",
                origin={"platform": "discord", "chat_id": home_channel},
            )

    # ── Orchestrator dispatch (PLAN decision 2): the background driver ─────

    def _concierge_dispatch_channel_object(self, channel_id):
        """The worker channel OBJECT (duck-typed off the hub's own client)
        for the reply fetch's anchor — ``None`` when unresolvable, which the
        driver's failure ladder treats like any other missing reply."""
        client = getattr(self, "_client", None)
        if client is None or channel_id is None:
            return None
        try:
            return client.get_channel(int(channel_id))
        except Exception:
            return None

    @staticmethod
    def _concierge_worker_reply_from_window(window):
        """The worker's answer: the NEWEST shared-identity MESSAGE of the
        worker channel's history window — the newest ``[Name [bot]]`` author
        line PLUS every line after it up to the next author line (or the
        window's end), so a multi-paragraph reply is captured WHOLE (the
        per-line read truncated it to its first paragraph and phase B
        summarized from a cut-down answer — the quality-review fix).
        ``None`` for an empty/unparseable window — the caller treats that as
        the worker-turn failure it equals."""
        if not isinstance(window, str):
            return None
        lines = window.splitlines()
        start_match = None
        start_idx = -1
        for i in range(len(lines) - 1, -1, -1):
            match = _WORKER_REPLY_LINE_RE.match(lines[i].strip())
            if match is not None:
                start_match, start_idx = match, i
                break
        if start_match is None:
            return None
        reply_lines = [start_match.group(1)]
        for line in lines[start_idx + 1:]:
            if _WORKER_REPLY_AUTHOR_LINE_RE.match(line) is not None:
                break  # the next history message opens: the reply is done
            reply_lines.append(line)
        return "\n".join(reply_lines).rstrip() or None

    async def _concierge_orchestrator_dispatch(self, event, task: str,
                                               blueprint_key: str) -> None:
        """The Discord-mediated dispatch driver (PLAN decision 2): run the
        worker's turn and the orchestrator's phase-B summary as REAL Discord
        turns, visibly, through the room machinery — never in-process.

        Serialized per home channel (``_concierge_room_lock_for``, the
        fan-out's queue-behind contract) and tracked like a fan-out driver.
        Fail-closed ladder, mirrored from the plan:

        - PRE-HANDOFF prepare failures (registry entry, worker provision,
          worker channel): warn ONCE and post ONE best-effort
          "⚠ dispatch failed" notice into the home channel — the wrap already
          suppressed the directive, so without the notice the user would see
          nothing at all (the quality-review fix). The HANDOFF post failure
          keeps the quiet abort: the handoff is the visible boundary, and a
          channel that could not take the handoff could not take a notice
          either.
        - POST-HANDOFF: a worker-turn or reply-fetch failure posts the
          "⚠ <worker> is unavailable" notice and phase B STILL runs (with the
          failure noted in its context) so the user is never left with just
          the handoff line; a phase-B failure posts the orchestrator's own
          unavailable notice. Never a silent drop.
        """
        source = getattr(event, "source", None)
        user_id = str(getattr(source, "user_id", "") or "")
        home_channel = str(getattr(source, "chat_id", "") or "")
        lock = self._concierge_room_lock_for(home_channel)
        # Cancellation-safe cleanup (quality review): the wrap recorded THIS
        # driver under the channel's fan-out key at SPAWN time
        # (``_concierge_track_fanout_task``), so the entry must be removed
        # even when the driver is CANCELLED while still QUEUED on the lock —
        # the cleanup ``finally`` must wrap the lock ACQUISITION, not sit
        # inside it (a queued driver never reaches an in-lock finally). The
        # current-task ownership check is kept: a superseding driver's entry
        # is never stolen.
        try:
            async with lock:
                tasks = getattr(self, "_concierge_room_fanout_tasks", None) or {}
                if tasks.get(home_channel) is not asyncio.current_task():
                    tasks[home_channel] = asyncio.current_task()
                await self._concierge_dispatch_drive(
                    event, task, blueprint_key, user_id, home_channel)
        finally:
            tasks = getattr(self, "_concierge_room_fanout_tasks", None) or {}
            if tasks.get(home_channel) is asyncio.current_task():
                del tasks[home_channel]

    async def _concierge_dispatch_drive(self, event, task: str,
                                        blueprint_key: str, user_id: str,
                                        home_channel: str) -> None:
        """The driver body (one lock-held pass; see
        ``_concierge_orchestrator_dispatch`` for the ladder)."""
        import dataclasses

        from concierge import provision, registry
        from concierge.blueprints import CATALOG

        source = event.source

        # 1) the worker: registry-owned, lazily provisioned (channel-less,
        #    per-user, idempotent) — and 2) its channel: bound channels
        #    first, else lazily created in the HOME channel's category and
        #    bound once. File/flock work off the loop (the M5c discipline).
        try:
            entry = registry.get_user(user_id) or {}
            # belt-and-suspenders: ``source.profile`` IS the wrap's
            # guard-confirmed orchestrator; this fallback guards direct-call
            # misuse and fails open coherently (to that same profile) if the
            # registry entry vanished mid-flight.
            orchestrator = str(
                entry.get("orchestrator")
                or getattr(source, "profile", "") or "")
            if not orchestrator:
                # the wrap confirmed the orchestrator moments ago; a race
                # (retire/remove) must fail closed BEFORE the handoff
                raise RuntimeError("the orchestrator entry disappeared")
            guild_id = (entry.get("guild_id")
                        or getattr(source, "guild_id", None))
            worker = registry.worker_for_user(user_id, blueprint_key)
            if not worker:
                record = await asyncio.to_thread(
                    provision.create_bot, blueprint_key,
                    user_id=user_id, channel_id=None)
                worker = (record.get("profile")
                          if isinstance(record, dict) else None)
            if not worker:
                raise RuntimeError(
                    "worker provisioning returned no profile")
            worker_channel = next(
                iter(registry.channels_for_profile(worker)), None)
            if not worker_channel:
                worker_channel = await self._concierge_create_channel(
                    guild_id=guild_id,
                    category_id=self._concierge_event_category_id(event),
                    name=self._concierge_worker_channel_name(
                        blueprint_key, source))
                if not worker_channel:
                    raise RuntimeError("the worker channel failed to create")
                await asyncio.to_thread(
                    registry.bind_channel, worker_channel,
                    profile=worker, blueprint=blueprint_key,
                    guild_id=guild_id)
                # the create event's auto-offer must not fire on the hub's
                # own channel (the create helper already marked it; asserted
                # here so the suppression cannot depend on its internals)
                self._concierge_remember_hub_created_channel(worker_channel)
            title = str(
                getattr(CATALOG[blueprint_key], "title", "") or blueprint_key)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._concierge_warn_once(
                ("dispatch-prepare", user_id, blueprint_key),
                "[%s] concierge: preparing the %s dispatch for user %s "
                "failed (%s); nothing but the failure notice was posted",
                getattr(self, "name", "discord"), blueprint_key, user_id, e,
                exc_info=True)
            # The wrap already SUPPRESSED the directive, so without a notice
            # the user would see nothing at all (quality review): ONE
            # best-effort "⚠ dispatch failed" post into the home channel.
            # The HANDOFF subcase below keeps its quiet abort — the handoff
            # post is the visible boundary, and a channel that could not
            # take the handoff could not take a notice either. Best-effort: a
            # notice failure must never raise out of the driver.
            try:
                channel = getattr(getattr(event, "raw_message", None),
                                  "channel", None)
                if channel is not None and hasattr(channel, "send"):
                    await channel.send("⚠ dispatch failed")
            except Exception as notice_error:
                logger.warning(
                    "[%s] concierge: the dispatch-failed notice for channel "
                    "%s could not be posted: %s",
                    getattr(self, "name", "discord"), home_channel,
                    notice_error)
            return

        # 3) the visible handoff — the point of no return. The raw message's
        #    channel is the home channel (the member-unavailable posture).
        try:
            channel = getattr(getattr(event, "raw_message", None),
                              "channel", None)
            if channel is None or not hasattr(channel, "send"):
                raise RuntimeError("the event carried no postable channel")
            await channel.send(f"→ asked **{title}**: {task}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._concierge_warn_once(
                ("dispatch-handoff", home_channel),
                "[%s] concierge: posting the dispatch handoff into channel "
                "%s failed (%s); the dispatch aborted with nothing else "
                "posted",
                getattr(self, "name", "discord"), home_channel, e,
                exc_info=True)
            return

        # 4) the worker's turn — the member-turn clone pattern, on the WORKER
        #    profile + channel with the TASK as the message text and the home
        #    window as context. NO _VOICE_RELAY_ATTR stamp: the worker's raw
        #    answer is never spoken (the phase-B summary is the single
        #    playback; the auto-TTS gate also keys on the worker channel,
        #    which no voice session joined).
        worker_event = None
        home_history = None
        try:
            home_history = await self._concierge_fetch_room_history(event)
            worker_source = dataclasses.replace(
                source, profile=worker, chat_id=str(worker_channel))
            worker_event = dataclasses.replace(
                event, source=worker_source, text=task,
                raw_message=_DispatchFetchAnchor(
                    self._concierge_dispatch_channel_object(worker_channel),
                    _dispatch_fetch_anchor_id()))
            if home_history is not None:
                worker_event.channel_context = home_history
            worker_event.media_urls = list(worker_event.media_urls or [])
            worker_event.media_types = list(worker_event.media_types or [])
            worker_event.media_text_inlined = list(
                worker_event.media_text_inlined or [])
            worker_event.metadata = dict(worker_event.metadata or {})
            await super().handle_message(worker_event)
            await self._concierge_wait_for_member_turn(worker_event)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "[%s] concierge: the %s worker turn for user %s failed "
                "(%s); the dispatch continues with the unavailable notice",
                getattr(self, "name", "discord"), blueprint_key, user_id, e,
                exc_info=True)
            worker_event = None

        # 5) the worker's reply: the newest shared-identity MESSAGE of the
        #    worker channel's window (its author line through the next author
        #    line — a multi-paragraph answer arrives WHOLE). A turn that ran
        #    but answered nothing, or a window that cannot be
        #    fetched/parsed, is the SAME failure the raised turn is —
        #    notice + phase B with the failure noted.
        #    The fetch anchor is re-minted HERE (fresh channel object, fresh
        #    ahead-in-time id): at clone time the channel may not have been
        #    cache-resolved yet, and the reply only exists since the turn.
        worker_answer = None
        if worker_event is not None:
            worker_event.raw_message = _DispatchFetchAnchor(
                self._concierge_dispatch_channel_object(worker_channel),
                _dispatch_fetch_anchor_id())
            worker_answer = self._concierge_worker_reply_from_window(
                await self._concierge_fetch_room_history(worker_event))
        if worker_answer is None:
            await self._concierge_post_member_unavailable(event, worker)

        # 6) phase B: the orchestrator summarizes. The clone keeps the
        #    ORIGINAL user text and the home channel; the context appends
        #    the worker's reply (or the failure note) as a labeled block so
        #    the turn sees BOTH the request and the reply. The continuation
        #    stamp goes on AFTER the replaces — its presence is what makes
        #    this a depth-1 turn (the wrap never re-dispatches it). The
        #    response is delivered into the home channel (and spoken, on a
        #    voice event) by the normal machinery — exactly one playback.
        try:
            phase_b_source = dataclasses.replace(
                source, profile=orchestrator, chat_id=home_channel)
            phase_b_event = dataclasses.replace(event, source=phase_b_source)
            reply_block = (
                f"[{title} replied]\n{worker_answer}"
                if worker_answer is not None else
                f"[{title} is unavailable]\n"
                f"(the dispatch could not reach {title})")
            phase_b_event.channel_context = (
                f"{home_history}\n\n{reply_block}"
                if home_history else reply_block)
            phase_b_event.media_urls = list(phase_b_event.media_urls or [])
            phase_b_event.media_types = list(phase_b_event.media_types or [])
            phase_b_event.media_text_inlined = list(
                phase_b_event.media_text_inlined or [])
            phase_b_event.metadata = dict(phase_b_event.metadata or {})
            setattr(phase_b_event, _CONCIERGE_DISPATCH_ATTR, True)
            await super().handle_message(phase_b_event)
            await self._concierge_wait_for_member_turn(phase_b_event)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "[%s] concierge: the phase-B summary turn for user %s "
                "failed (%s); posting the orchestrator unavailable notice",
                getattr(self, "name", "discord"), user_id, e, exc_info=True)
            await self._concierge_post_member_unavailable(event, orchestrator)

    def _concierge_track_fanout_task(self, channel_id: str, task) -> None:
        """Record the in-flight fan-out driver per channel (observability +
        tests) and register it with the adapter's background-task set so
        shutdown cancels it cleanly. Lazy state: the mixin must not require
        an ``__init__`` (duck-typed onto ``object.__new__`` adapters)."""
        tasks = getattr(self, "_concierge_room_fanout_tasks", None)
        if tasks is None:
            tasks = self._concierge_room_fanout_tasks = {}
        tasks[channel_id] = task
        background = getattr(self, "_background_tasks", None)
        if background is not None:
            try:
                background.add(task)
                task.add_done_callback(background.discard)
            except Exception:
                pass

    def _concierge_room_lock_for(self, channel_id: str):
        """Per-channel fan-out serialization: a second room message that
        arrives while a fan-out is running waits for it (desktop-room parity:
        turns queue behind the active drive), instead of interleaving two
        fan-outs' member turns."""
        locks = getattr(self, "_concierge_room_locks", None)
        if locks is None:
            locks = self._concierge_room_locks = {}
        lock = locks.get(channel_id)
        if lock is None:
            lock = locks[channel_id] = asyncio.Lock()
        return lock

    async def _concierge_run_room_turns(self, event, targets) -> None:
        """The fan-out driver: run each member's turn SEQUENTIALLY in stored
        order. Per member (the plan's error-isolation requirement): a
        dispatch/turn error posts ONE "⚠ <bot> is unavailable" into the
        channel and the remaining members still run. Cancellation (shutdown)
        propagates — the driver is a background task like any turn.

        The room history window is fetched ONCE here, before the first
        member dispatches, and shared read-only across the member turns:
        every member's window is anchored at the same trigger, so per-member
        fetches would re-walk identical discord.py I/O N times."""
        channel_id = str(
            getattr(getattr(event, "source", None), "chat_id", "") or "")
        lock = self._concierge_room_lock_for(channel_id)
        # Cancellation-safe cleanup (quality review): the wrap recorded THIS
        # driver under the channel's fan-out key at SPAWN time, so the entry
        # must be removed even when the driver is CANCELLED while still
        # QUEUED on the lock — the cleanup ``finally`` must wrap the lock
        # ACQUISITION, not sit inside it (the dispatch driver's invariant).
        try:
            async with lock:
                tasks = getattr(self, "_concierge_room_fanout_tasks", None) or {}
                if tasks.get(channel_id) is not asyncio.current_task():
                    tasks[channel_id] = asyncio.current_task()
                room_history = await self._concierge_fetch_room_history(event)
                for member in targets:
                    try:
                        await self._concierge_run_member_turn(
                            event, member, room_history)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.error(
                            "[%s] concierge: room member %s failed for "
                            "channel %s: %s",
                            getattr(self, "name", "discord"), member,
                            channel_id, e, exc_info=True)
                        await self._concierge_post_member_unavailable(
                            event, member)
        finally:
            tasks = getattr(self, "_concierge_room_fanout_tasks", None) or {}
            if tasks.get(channel_id) is asyncio.current_task():
                del tasks[channel_id]

    async def _concierge_run_member_turn(self, event, member: str,
                                         room_history: str | None = None) -> None:
        """One member's turn, through the SAME machinery a single-bot
        message takes: a fresh source stamped ``profile = member`` (the M5b
        stamping mechanism — ``dataclasses.replace`` so the cached routing
        identity is resolved for THIS member, not reused; the plain-attr
        concierge stamps do not survive the replace, so the member event is
        a pure delegate through the wrap, never a nested fan-out) and a fresh
        event (fresh media/metadata containers: a busy-session merge must
        never mutate a sibling member's event), dispatched via the REAL
        ``super().handle_message`` — canonicalization, profile-namespaced
        session key, busy guard, and (through ``_process_message_background``
        → the ``set_message_handler`` wrap) the runner's admission, scope
        and turn machinery, plus the adapter-side response delivery, all run
        as for any bound-channel message. Direct-calling the stored runner
        handler instead would SKIP that delivery lane (the runner handler
        only RETURNS the response — ``_process_message_background`` is what
        posts it), so the base dispatch stays. Then WAIT for that member's
        session task before returning (sequentiality).

        ``room_history`` (the fan-out's shared channel window, sibling
        members' replies included) is stamped as the member event's
        ``channel_context`` — the same field the standard backfill
        populates. ``None`` (fetch failed / no channel to fetch from) keeps
        whatever context the cloned event carried: history must never fail
        the fan-out."""
        import dataclasses

        member_source = dataclasses.replace(
            event.source, profile=member)
        member_event = dataclasses.replace(event, source=member_source)
        if room_history is not None:
            # The cloned event carries the STANDARD backfill's context, which
            # self-partitions at the shared identity — under the concierge's
            # single Discord identity that hides every sibling member's
            # reply. The room window replaces it ("" = an honestly empty
            # window: nothing conversational happened before the trigger).
            member_event.channel_context = room_history
        member_event.media_urls = list(member_event.media_urls or [])
        member_event.media_types = list(member_event.media_types or [])
        member_event.media_text_inlined = list(member_event.media_text_inlined or [])
        member_event.metadata = dict(member_event.metadata or {})
        # The voice-relay stamp goes on AFTER the replaces (plain attrs do
        # not survive dataclasses.replace): the auto-TTS seam in
        # ConciergeVoiceMixin reads it to tell a member-lane voice event
        # from the hub's own spoken turn and relay the answer as a summary.
        setattr(member_event, _VOICE_RELAY_ATTR, member)
        await super().handle_message(member_event)
        await self._concierge_wait_for_member_turn(member_event)

    async def _concierge_fetch_room_history(self, event) -> str | None:
        """The room fan-out's channel-history window: recent messages BEFORE
        the trigger from ALL conversational authors — humans AND the shared
        bot identity, i.e. sibling members' replies — with NO self-partition
        (the standard ``_fetch_channel_context`` breaks at the bot's own last
        message, which under the concierge's single identity would hide
        every member reply; see the module docstring's History section).

        Windowing (desktop-room parity): the newest
        ``_ROOM_HISTORY_MESSAGE_CAP`` messages, further truncated from the
        OLDEST end to stay under ``_ROOM_HISTORY_CHAR_CAP`` characters.

        Returns the ``[Recent channel messages]`` block ("" when the channel
        has no conversational history before the trigger), or ``None`` when
        the window could not be fetched — no channel on the event, or the
        discord.py history call failed (permissions, deleted channel), which
        logs ONE warning per fan-out. Never raises: history must never fail
        the fan-out."""
        raw = getattr(event, "raw_message", None)
        channel = getattr(raw, "channel", None)
        if channel is None or not hasattr(channel, "history"):
            return None
        channel_id = str(getattr(channel, "id", "?"))
        is_thread_channel = self._concierge_channel_is_thread(channel)
        try:
            lines: list[str] = []
            has_unverified = False
            # Newest-first scan (discord.py's default direction), anchored
            # BEFORE the trigger so the trigger stays the "[New message]"
            # the turn itself prepends. The message cap is enforced in code
            # as well as via ``limit``: a history source that ignores
            # ``limit`` must not be able to blow the window.
            async for msg in channel.history(
                    limit=_ROOM_HISTORY_MESSAGE_CAP, before=raw,
                    oldest_first=False):
                line, unverified = self._concierge_room_history_line(
                    msg, channel_id, is_thread_channel)
                if line is None:
                    continue
                lines.append(line)
                has_unverified = has_unverified or unverified
            if not lines:
                return ""
            lines = lines[:_ROOM_HISTORY_MESSAGE_CAP]
            # Char cap: whole lines dropped from the OLDEST end; the NEWEST
            # line always survives (a window, not a filter — one oversized
            # line alone is still the member's most recent context). ``lines``
            # is newest-first, so the walk starts at the newest line.
            kept: list[str] = []
            total = 0
            for line in lines:
                if kept and total + len(line) + 1 > _ROOM_HISTORY_CHAR_CAP:
                    break
                kept.append(line)
                total += len(line) + 1
            kept.reverse()  # newest-first → chronological
            from plugins.platforms.discord.adapter import (
                _UNVERIFIED_HISTORY_PREAMBLE,
            )

            blocks: list[str] = []
            if has_unverified:
                blocks.append(_UNVERIFIED_HISTORY_PREAMBLE)
            blocks.append("[Recent channel messages]\n" + "\n".join(kept))
            return "\n\n".join(blocks)
        except Exception as e:
            logger.warning(
                "[%s] concierge: room history fetch failed for channel %s; "
                "member turns fall back to the standard channel context: %s",
                getattr(self, "name", "discord"), channel_id, e)
            return None

    def _concierge_room_history_line(self, msg, channel_id: str,
                                     is_thread_channel: bool) -> tuple[str | None, bool]:
        """Format one history message for the room window — the adapter's ONE
        canonical line formatter (``_format_channel_history_line``, the same
        function the standard ``_fetch_channel_context`` window formats
        through: ``[name] content``, ``[bot]`` suffix for bot authors,
        ``[unverified]`` prefix for non-allowlisted humans, foreign bots
        behind DISCORD_ALLOW_BOTS), so the two windows' injection contracts
        cannot drift. The shared identity's own messages are KEPT: they are
        the sibling members' replies, the whole point of the window — the
        keep-vs-partition POLICY stays with the callers (the room scan never
        breaks at the shared identity; the standard primary window does).

        Duck-typed onto the adapter (the mixin must not require adapter
        ``__init__`` state): every adapter helper is optional, and a missing
        one degrades to the conservative default, never to a crash. A broken
        formatter import propagates to ``_concierge_fetch_room_history``'s
        handler — members fall back to the standard backfill's context with
        ONE warning, the same posture as a failed fetch."""
        from plugins.platforms.discord.adapter import _format_channel_history_line

        try:
            allow_bots = getattr(self, "_get_allow_bots", None)
            include_other_bots = (
                callable(allow_bots) and allow_bots() != "none")
        except Exception:
            include_other_bots = False
        return _format_channel_history_line(
            msg,
            self_user=getattr(getattr(self, "_client", None), "user", None),
            include_other_bots=include_other_bots,
            nonconversational_ids=getattr(
                self, "_nonconversational_messages", None),
            auth_check=getattr(self, "_is_sender_authorized", None),
            chat_type="thread" if is_thread_channel else "group",
            chat_id=channel_id,
        )

    @staticmethod
    def _concierge_channel_is_thread(channel) -> bool:
        """Best-effort ``isinstance(channel, discord.Thread)`` for the
        unverified tag's chat_type — False when discord.py (or its test
        double) cannot answer."""
        try:
            import discord

            return isinstance(channel, discord.Thread)
        except Exception:
            return False

    @staticmethod
    def _concierge_channel_is_forum(channel) -> bool:
        """Best-effort forum-channel detection for the channel-create
        auto-offer skip — the same verdict the adapter's
        ``_is_forum_parent`` gives (``isinstance`` against
        ``discord.ForumChannel``, else Discord's channel-type value 15),
        duck-typed so the mixin stays importable and testable without the
        adapter class present. False when discord.py or the channel cannot
        answer: the picker is attempted exactly as before (never a silent
        skip on an unknowable channel)."""
        if channel is None:
            return False
        try:
            import discord

            forum_cls = getattr(discord, "ForumChannel", None)
            if forum_cls is not None and isinstance(channel, forum_cls):
                return True
        except Exception:
            return False
        channel_type = getattr(channel, "type", None)
        if channel_type is None:
            return False
        type_value = getattr(channel_type, "value", channel_type)
        return type_value == 15

    async def _concierge_wait_for_member_turn(self, member_event) -> None:
        """Wait until the member's session is quiescent (its turn — and any
        follow-up drain that replaced the task — finished) before the next
        member dispatches. Bounded by ``_ROOM_MEMBER_TURN_WAIT_SECONDS``:
        giving up stops WAITING only (the turn is ``asyncio.shield``-ed,
        never cancelled) and logs loudly — a still-running member is slow,
        not failed, so no "unavailable" notice is posted for it. A member
        whose turn task RAISED propagates here: the driver answers with the
        unavailable notice."""
        session_key = None
        try:
            session_key = self._event_session_key(member_event)
        except Exception:
            session_key = None
        if not session_key:
            return
        session_tasks = getattr(self, "_session_tasks", None) or {}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _ROOM_MEMBER_TURN_WAIT_SECONDS
        while True:
            task = session_tasks.get(session_key)
            if task is None or getattr(task, "done", lambda: True)():
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning(
                    "[%s] concierge: room member turn for %s still running "
                    "after %.0fs; continuing the fan-out without waiting",
                    getattr(self, "name", "discord"), session_key,
                    _ROOM_MEMBER_TURN_WAIT_SECONDS)
                return
            # shield: a wait timeout must never cancel the member's turn.
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)

    async def _concierge_post_member_unavailable(self, event, member: str) -> None:
        """The plan's error-isolation notice: ONE "⚠ <bot> is unavailable"
        into the channel, then the fan-out continues. Best effort by design
        (a notice failure must not eat the remaining members): duck-typed
        onto the triggering message's channel, logged when undeliverable."""
        text = f"⚠ **{self._concierge_member_title(member)}** is unavailable"
        channel = getattr(getattr(event, "raw_message", None), "channel", None)
        try:
            if channel is not None and hasattr(channel, "send"):
                await channel.send(text)
                return
        except Exception as e:
            logger.warning(
                "[%s] concierge: could not post the unavailable notice for "
                "room member %s: %s",
                getattr(self, "name", "discord"), member, e)
            return
        logger.warning(
            "[%s] concierge: no channel to post the unavailable notice for "
            "room member %s (raw message carried none)",
            getattr(self, "name", "discord"), member)

    def _concierge_member_title(self, profile: str) -> str:
        """Display title for a member profile: the ``profile.yaml``
        description (provisioning writes the blueprint title there —
        ``create_profile(description=bp.title)``), falling back to the
        profile name. Never raises; never on the hot path."""
        try:
            from hermes_cli.profiles import get_profile_dir, read_profile_meta

            meta = read_profile_meta(get_profile_dir(profile)) or {}
            title = str(meta.get("description") or "").strip()
            if title:
                return title
        except Exception:
            pass
        return profile

    def _concierge_route_for_source(self, source) -> str | None:
        """Legacy single-lookup shape (kept for direct callers/tests): load
        the registry and resolve the single-bot binding verdict."""
        reg = self._concierge_registry_or_none(source)
        if reg is None:
            return None
        return self._concierge_profile_from_registry(source, reg)

    # ── Task 5.4/5.5/5.7: the command + event surface ─────────────────────
    #
    # Registration-time gates mirror the per-message ones: disabled concierge
    # means NO surface at all (no /bot group, no channel-create handler, and
    # the thread chain is a straight pass-through) — the zero-behavior-change
    # rule for the ~every existing install running with concierge off.

    def _concierge_settings_obj(self):
        """The frozen ConciergeSettings for THIS runner's in-hand config (or
        ``None`` when unreadable — already warned once per process by the
        gate-1 helper). Event/command paths use the settings' managed-category
        list; a None settings means "treat as unmanaged": no auto-offer."""
        try:
            from concierge.settings import concierge_settings

            config = getattr(getattr(self, "gateway_runner", None), "config", None)
            section = getattr(config, "concierge", None)
            return concierge_settings(
                {"concierge": section} if isinstance(section, dict) else None)
        except Exception:
            return None

    def _concierge_free_response_channels(self):
        """Bound + room channel ids for the adapter's free-response union.

        The registry is the routing source of truth: a channel with a bot
        binding or a room membership OWNS its conversation, so the hub
        identity must answer there without an @mention (admission's
        no-mention drop would otherwise make every provisioned bot
        unreachable — the 2026-09-27 live E2E bug). Runs on the admission
        path, so it must be cheap and NEVER raise (unreadable registry =
        no extra channels, one warning per path is the adapter's)."""
        try:
            from concierge import registry

            reg = registry.load_registry()
            channels = set(reg.get("bindings", {}).keys())
            channels.update(reg.get("rooms", {}).keys())
            return channels
        except Exception:
            logger.debug("concierge free-response lookup failed", exc_info=True)
            return ()

    def _concierge_free_response_message(self, message) -> bool:
        """True when *message*'s channel sits in a concierge MANAGED category.

        The orchestrator front door's channels live in managed categories
        and must answer plain typing (no @mention): the id-based hook above
        covers only bound/room channels, so an UNBOUND managed-category
        channel fell out of the union and ``require_mention`` (default on)
        dropped the message at ingest — upstream of the concierge wrap, so
        the T4 fallback/bootstrap never fired (the 2026-11-14 live E2E
        bug). Bound/room channels are already covered by the id-based hook,
        making this a clean superset rule: "a channel in a concierge
        managed category is free-response".

        Fail-closed, mirroring ``_concierge_event_in_managed_category``:
        anything unresolvable resolves False — disabled concierge
        (``_concierge_events_wanted``), unreadable/None settings, or no
        category on the channel. THREADS don't resolve a category (the
        parent channel's category is not reachable from the thread object),
        so a thread message in a managed category is NOT free-response by
        this hook — the same fail-closed limitation T4 documented. Runs on
        the admission path, so it must be cheap and NEVER raise.
        """
        try:
            if not self._concierge_events_wanted():
                return False
            settings = self._concierge_settings_obj()
            if settings is None:
                return False
            channel = getattr(message, "channel", None)
            category_id = getattr(channel, "category_id", None)
            if category_id is None:
                category_id = getattr(getattr(channel, "category", None),
                                      "id", None)
            if category_id is None:
                return False
            managed = {str(c) for c in (settings.managed_category_ids or ())}
            return str(category_id) in managed
        except Exception:
            logger.debug("concierge free-response message lookup failed",
                         exc_info=True)
            return False

    def _concierge_events_wanted(self) -> bool:
        """Gate for the whole command/event surface: enabled AND multiplexed
        (the same two gates routing applies — commands that bind channels are
        pointless in a single-profile gateway, and a created channel's bot
        could never run)."""
        runner = getattr(self, "gateway_runner", None)
        if not _concierge_enabled_from_config(runner):
            return False
        return bool(getattr(getattr(runner, "config", None), "multiplex_profiles", False))

    # ── Admission's dynamic runtime-profile resolver ─────────────────────────
    #
    # The live bound-channel admission bug (2026-10): ``build_source`` stamps
    # ``source.profile`` with the registry's binding verdict, but the ADMISSION
    # machinery (gateway/session_ingress_context.py) resolved the runtime profile
    # from ``gateway.profile_routes`` alone — a registry-routed source has no
    # matching route, so it resolved to the adapter's owner and died at the
    # ``profile_mismatch`` guard. Rooms make static routes impossible (one
    # channel, up to six member profiles → same-specificity admission_conflict),
    # so the registry itself must answer — via a duck-typed runner attribute
    # (the ``_concierge_free_response_channels`` precedent), keeping core free
    # of any plugin import.

    def _concierge_dynamic_runtime_profile(self, source, homes) -> str | None:
        """The runtime profile a REGISTRY-STAMPED source admits to — the stamp
        itself, but ONLY when the registry still confirms it:

        (a) the source's channel — or its parent, threads/forum posts inherit
            the parent's binding (``_concierge_binding_key``'s key resolution)
            — is bound to exactly ``source.profile``, or
        (b) the channel (or parent) is a ROOM with ``source.profile`` among
            its members, or
        (c) the channel (or parent) is bound to nothing and rooms nowhere,
            and the USERS section gives the author (``source.user_id``) an
            orchestrator exactly equal to ``source.profile`` — the
            unbound→orchestrator fallback's re-home (design §4; the wrap
            re-homes the source to the author's orchestrator before
            delegation, and this branch is what admits it).

        Anything else → None: admission keeps its owner fallback, so the
        fail-closed guard refuses (an unbound channel answers as the hub
        naturally; a stamp the registry does not confirm is a spoof, never a
        route). Registry unreadable → None (fail closed). Read-only: no
        registry writes, and ``homes`` is the caller's contract — ``_binding``
        still enforces served-home membership and the authority check.
        """
        if getattr(getattr(source, "platform", None), "value", None) != "discord":
            return None  # the registry speaks Discord channel ids only
        stamp = getattr(source, "profile", None)
        if not isinstance(stamp, str) or not stamp:
            return None
        try:
            from concierge import registry

            reg = registry.load_registry()
            bindings = reg.get("bindings")
            if isinstance(bindings, dict):
                key = self._concierge_binding_key(source, bindings)
                if key is not None:
                    binding = bindings.get(key)
                    # Bound to a DIFFERENT profile than the stamp: not registry
                    # truth. A bound channel is a binding XOR a room, so no
                    # room fallthrough either.
                    if isinstance(binding, dict) and binding.get("profile") == stamp:
                        return stamp
                    return None
            rooms = reg.get("rooms")
            if isinstance(rooms, dict):
                for candidate in (getattr(source, "chat_id", None),
                                  getattr(source, "parent_chat_id", None)):
                    cid = str(candidate) if candidate is not None else ""
                    if not cid:
                        continue
                    room = rooms.get(cid)
                    if not isinstance(room, dict):
                        continue  # not a room under this key: try the parent
                    members = room.get("members")
                    if isinstance(members, list) and stamp in members:
                        return stamp
                    return None  # the chat's room does not include the stamp
            # (c): an unbound, non-room channel whose author's registry
            # orchestrator IS the stamp. Reached only past the binding/room
            # blocks above (both return early for claimed channels), so a
            # users entry can never bypass a binding or a room membership.
            users = reg.get("users")
            if isinstance(users, dict):
                entry = users.get(str(getattr(source, "user_id", "") or ""))
                if (isinstance(entry, dict)
                        and entry.get("orchestrator") == stamp):
                    return stamp
        except Exception:
            logger.debug(
                "[%s] concierge: dynamic runtime-profile resolution failed; "
                "admission falls back to the owner profile",
                getattr(self, "name", "discord"), exc_info=True)
            return None
        return None

    def _concierge_wire_dynamic_router(self, runner=None) -> None:
        """Install the resolver on the gateway runner (idempotent; re-assign is
        fine). The runner attribute is the duck-typed hook core reads — the
        only seam that does not make gateway/session_ingress_context.py import
        the plugin layer. Self-gated: disabled concierge or a non-multiplexed
        gateway installs nothing (zero behavior change, the MRO rule). Called
        from the adapter's connect path AND from ``build_source`` after its
        gates pass (belt and suspenders: per-message re-check is one identity
        compare)."""
        runner = runner if runner is not None else getattr(self, "gateway_runner", None)
        if runner is None:
            return
        # ONE stable bound method per adapter (a fresh ``self.method`` access is
        # a new object each time, so the identity check below would never hit).
        hook = getattr(self, "_concierge_runtime_profile_hook", None)
        if hook is None:
            hook = self._concierge_runtime_profile_hook = self._concierge_dynamic_runtime_profile
        if getattr(runner, "_dynamic_runtime_profile", None) is hook:
            return  # already wired: cheap per-message identity check
        if not self._concierge_events_wanted():
            return
        runner._dynamic_runtime_profile = hook
        logger.debug(
            "[%s] concierge: dynamic runtime-profile resolver installed on the gateway runner",
            getattr(self, "name", "discord"))

    def _concierge_register_slash(self, tree) -> None:
        """Add the ``/bot`` command group to the tree (Task 5.4).

        Called from the adapter's ``_register_slash_commands`` AFTER its own
        registrations, so a name collision with a native command keeps the
        native one (the concierge group loses its slot and logs once — never
        breaks the sync). Self-gating: disabled concierge (or a
        single-profile gateway) adds nothing to the tree."""
        import discord

        if not self._concierge_events_wanted():
            return

        try:
            group = discord.app_commands.Group(
                name="bot", description="Set up and manage this server's helper bots")
        except Exception:
            self._concierge_warn_once(
                ("slash-group",), "concierge: could not build the /bot command group")
            return

        @discord.app_commands.describe()
        async def _new(interaction: discord.Interaction):
            await self._concierge_cmd_new(interaction)

        @discord.app_commands.describe(bot="The bot to retire (from /bot list)")
        async def _retire(interaction: discord.Interaction, bot: str):
            from concierge.auth_tiers import is_admin
            if not is_admin(getattr(getattr(interaction, "user", None), "id", None)):
                await self._concierge_respond(interaction, "That command is admin-only.", ephemeral=True)
                return
            await self._concierge_cmd_retire(interaction, bot)

        async def _list(interaction: discord.Interaction) -> None:
            await self._concierge_cmd_list(interaction)

        async def _memory(interaction: discord.Interaction) -> None:
            await self._concierge_cmd_memory(interaction)

        _new.__name__ = "concierge_bot_new"
        _retire.__name__ = "concierge_bot_retire"
        _list.__name__ = "concierge_bot_list"
        _memory.__name__ = "concierge_bot_memory"
        group.add_command(discord.app_commands.Command(
            name="new", description="Pick a helper bot for a channel",
            callback=_new))
        group.add_command(discord.app_commands.Command(
            name="list", description="Show this server's bots",
            callback=_list))
        group.add_command(discord.app_commands.Command(
            name="memory", description="What your bots remember about you",
            callback=_memory))
        group.add_command(discord.app_commands.Command(
            name="retire", description="Retire a bot (pauses it everywhere)",
            callback=_retire))
        async def _category(interaction: discord.Interaction, user: str):
            from concierge.auth_tiers import is_admin
            if not is_admin(getattr(getattr(interaction, "user", None), "id", None)):
                await self._concierge_respond(interaction, "That command is admin-only.", ephemeral=True)
                return
            await self._concierge_cmd_category(interaction, user)
        _category.__name__ = "concierge_bot_category"
        group.add_command(discord.app_commands.Command(
            name="category", description="Create a private category for a user (admin-only)",
            callback=_category))
        try:
            tree.add_command(group)
        except Exception:
            # e.g. a plugin already registered a /bot group: it wins.
            self._concierge_warn_once(
                ("slash-conflict",),
                "concierge: a /bot command already exists on the tree; the "
                "concierge /bot group was not registered")

        # ── Task 7.4: the /room group, after /bot (same self-gating) ──────
        try:
            room_group = discord.app_commands.Group(
                name="room",
                description="Manage bot rooms (multi-bot channels)")
        except Exception:
            self._concierge_warn_once(
                ("slash-group-room",),
                "concierge: could not build the /room command group")
            return

        @discord.app_commands.describe(
            topic="Optional topic — names the room's channel")
        async def _room_create(interaction: discord.Interaction,
                               topic: str | None = None):
            await self._concierge_cmd_room_create(interaction, topic)

        @discord.app_commands.describe(bot="The bot to add (from /bot list)")
        async def _room_add(interaction: discord.Interaction, bot: str):
            from concierge.auth_tiers import is_admin
            if not is_admin(getattr(getattr(interaction, "user", None), "id", None)):
                await self._concierge_respond(interaction, "That command is admin-only.", ephemeral=True)
                return
            await self._concierge_cmd_room_add(interaction, bot)

        @discord.app_commands.describe(bot="The bot to remove")
        async def _room_remove(interaction: discord.Interaction, bot: str):
            from concierge.auth_tiers import is_admin
            if not is_admin(getattr(getattr(interaction, "user", None), "id", None)):
                await self._concierge_respond(interaction, "That command is admin-only.", ephemeral=True)
                return
            await self._concierge_cmd_room_remove(interaction, bot)

        async def _room_disband(interaction: discord.Interaction):
            from concierge.auth_tiers import is_admin
            if not is_admin(getattr(getattr(interaction, "user", None), "id", None)):
                await self._concierge_respond(interaction, "That command is admin-only.", ephemeral=True)
                return
            await self._concierge_cmd_room_disband(interaction)

        _room_create.__name__ = "concierge_room_create"
        _room_add.__name__ = "concierge_room_add"
        _room_remove.__name__ = "concierge_room_remove"
        _room_disband.__name__ = "concierge_room_disband"
        room_group.add_command(discord.app_commands.Command(
            name="create", description="Create a room of 2–6 bots (new channel)",
            callback=_room_create))
        room_group.add_command(discord.app_commands.Command(
            name="add", description="Add a bot to this channel's room",
            callback=_room_add))
        room_group.add_command(discord.app_commands.Command(
            name="remove", description="Remove a bot from this channel's room",
            callback=_room_remove))
        room_group.add_command(discord.app_commands.Command(
            name="disband", description="Disband this channel's room",
            callback=_room_disband))
        try:
            tree.add_command(room_group)
        except Exception:
            # e.g. a plugin already registered a /room group: it wins.
            self._concierge_warn_once(
                ("slash-conflict-room",),
                "concierge: a /room command already exists on the tree; the "
                "concierge /room group was not registered")

        # ── Task 8.2: /connect — account-connection deep links ───────────
        try:
            @discord.app_commands.describe(
                kind="Optional: the account to connect (e.g. email, calendar)")
            async def _connect(interaction: discord.Interaction,
                               kind: str | None = None):
                from concierge.auth_tiers import is_admin
                if not is_admin(getattr(getattr(interaction, "user", None), "id", None)):
                    await self._concierge_respond(interaction, "That command is admin-only.", ephemeral=True)
                    return
                await self._concierge_cmd_connect(interaction, kind)

            _connect.__name__ = "concierge_connect"
            tree.add_command(discord.app_commands.Command(
                name="connect",
                description="Connect the accounts your bots need (email, calendar)",
                callback=_connect))
        except Exception:
            # e.g. a plugin already registered /connect: it wins.
            self._concierge_warn_once(
                ("slash-conflict-connect",),
                "concierge: a /connect command already exists on the tree; "
                "the concierge /connect was not registered")

    # ── /bot new: picker → provision (Tasks 5.4 + 5.7 chained) ──────────────

    async def _concierge_cmd_category(self, interaction, user: str) -> None:
        """Create a private Discord category for a user (admin-only)."""
        import discord
        await self._concierge_defer(interaction, ephemeral=True)
        guild = getattr(interaction, "guild", None)
        if guild is None:
            await self._concierge_respond(interaction, "Must be used in a server.", ephemeral=True)
            return
        target_id = user.strip().lstrip("<@").rstrip(">").strip()
        try:
            target_id_int = int(target_id)
        except ValueError:
            await self._concierge_respond(interaction, "Invalid user id.", ephemeral=True)
            return
        try:
            member = guild.get_member(target_id_int) or await guild.fetch_member(target_id_int)
        except Exception:
            member = None
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        if member is not None:
            overwrites[member] = discord.PermissionOverwrite(view_channel=True, manage_channels=True)
        for m in getattr(guild, "members", []):
            if getattr(m, "guild_permissions", None) and m.guild_permissions.administrator:
                overwrites[m] = discord.PermissionOverwrite(view_channel=True, manage_channels=True)
        try:
            category = await guild.create_category(
                name=f"{member.display_name if member else target_id}-private",
                overwrites=overwrites,
            )
        except Exception as e:
            await self._concierge_respond(interaction, f"Failed to create category: {e}", ephemeral=True)
            return
        await self._concierge_respond(
            interaction, f"Created private category '{category.name}' (id {category.id}).",
            ephemeral=True)

    async def _concierge_cmd_new(self, interaction) -> None:
        """Ephemeral blueprint picker; the selection provisions the bot into
        the invoking channel (Task 5.4), via the Task 5.7 create-vs-bind plan
        when the invoking context is a managed category."""
        import discord

        if not await self._concierge_gate_interaction(interaction, "/bot new"):
            return
        payload = _concierge_payload("build_new_bot_response")()
        view = self._concierge_build_picker_view(payload["choices"])
        try:
            await interaction.response.send_message(
                payload["content"], view=view, ephemeral=True)
        except Exception as e:
            logger.warning("[%s] /bot new: picker send failed: %s",
                           getattr(self, "name", "discord"), e)

    def _concierge_build_picker_view(self, choices, *,
                                     prefer_create: bool = True,
                                     ephemeral: bool = True):
        """The blueprint select-menu View. Auth: the hub's user/role allowlists
        (transport-scoped, same as every component view in this adapter).

        ``prefer_create``/``ephemeral`` thread through the select callback
        into ``_concierge_provision_from_interaction``. The defaults are the
        ``/bot new`` semantics (a dedicated channel, ephemeral replies); the
        Task 5.5 auto-offer sites pass ``False``/``False`` — the channel the
        picker is posted in IS the destination, and the confirmation is
        public."""
        import discord

        view = discord.ui.View(timeout=300)
        options = [
            discord.SelectOption(
                label=c["label"][:100],
                value=c["value"],
                description=(c.get("description") or "")[:100],
            )
            for c in choices[:25]
        ]
        select = discord.ui.Select(
            placeholder="Pick a bot for this channel…", options=options)
        select.callback = self._concierge_make_picker_callback(
            view, prefer_create=prefer_create, ephemeral=ephemeral)
        view.add_item(select)
        return view

    def _concierge_make_picker_callback(self, view, *,
                                        prefer_create: bool = True,
                                        ephemeral: bool = True):
        """Bound callback for the picker's select: resolve the selection into
        a provisioned bot, threading the picker site's
        ``prefer_create``/``ephemeral`` into the provision step (see
        ``_concierge_build_picker_view``). Wrapped (not a View subclass) so
        the mixin stays duck-typed — ``_component_check_auth`` lives on the
        adapter module."""
        import discord

        async def _on_select(interaction: discord.Interaction) -> None:
            if not self._concierge_component_auth(interaction):
                await interaction.response.send_message(
                    "You're not allowed to use this.", ephemeral=True)
                return
            # Defer BEFORE any provisioning work: the provision (profile
            # copytree, cron-store write, a cross-process flock that can wait
            # up to 60s) can outlast the ~3s interaction token, and an
            # unanswered interaction burns it — defer-then-followup keeps
            # every outcome (success, error, lock timeout) deliverable. The
            # ephemeral flag threads from the picker site, so the auto-offer
            # pickers defer (and confirm) publicly, same as before.
            await self._concierge_defer(interaction, ephemeral=ephemeral)
            blueprint_key = str(
                (getattr(interaction, "data", {}) or {}).get("values", [""])[0])
            await self._concierge_provision_from_interaction(
                interaction, blueprint_key, source_view=view,
                ephemeral=ephemeral, prefer_create=prefer_create)

        return _on_select

    def _concierge_component_auth(self, interaction) -> bool:
        """User/role gate for concierge component clicks (picker, confirm
        buttons). Uses the adapter module's own ``_component_check_auth``
        (the same gate the adapter's approval/model-picker views use) with
        THIS adapter's allowlist sets; a bare/duck-typed adapter in tests may
        still provide ``_component_check_auth`` as an attribute, which wins."""
        checker = getattr(self, "_component_check_auth", None)
        if callable(checker):
            try:
                return bool(checker(
                    interaction,
                    getattr(self, "_allowed_user_ids", set()),
                    getattr(self, "_allowed_role_ids", set())))
            except Exception:
                return False
        # Upstream's approval-card refactor moved ``_component_check_auth`` out of
        # adapter.py into adapter_component_auth.py — resolve it there (still the
        # same gate the adapter's approval/model-picker views use).
        try:
            from plugins.platforms.discord.adapter_component_auth import _component_check_auth
            return bool(_component_check_auth(
                interaction,
                getattr(self, "_allowed_user_ids", set()),
                getattr(self, "_allowed_role_ids", set())))
        except Exception:
            return False

    async def _concierge_provision_from_interaction(
        self, interaction, blueprint_key: str, *, source_view=None,
        ephemeral: bool = True, prefer_create: bool = False,
    ) -> None:
        """Provision ``blueprint_key`` for the interaction's channel, through
        the Task 5.7 create-vs-bind plan. Shared by the ``/bot new`` picker
        (ephemeral replies, ``prefer_create=True``) and the Task 5.5
        auto-offer pickers (non-ephemeral, ``prefer_create=False`` — the
        channel the picker was posted in IS the destination); both flags are
        threaded from the picker site through
        ``_concierge_build_picker_view``/``_concierge_make_picker_callback``.
        When the plan CREATES a channel and provisions into it, a
        non-ephemeral greeting is posted there afterwards (plan 5.7: "channel
        appears, bot says hi") — best effort, never a provisioning failure.

        Failure copy is user-language ("couldn't set that up — try again"),
        never a Hermes stack trace: provisioning errors are logged with full
        detail on the hub side."""
        from concierge.provider_groups import resolve_provider_group

        channel_id = self._concierge_interaction_channel_id(interaction)
        guild_id = self._concierge_interaction_guild_id(interaction)
        category_id = self._concierge_interaction_category_id(interaction)
        settings = self._concierge_settings_obj()

        plan = self._concierge_plan_channel(
            channel_id=channel_id, category_id=category_id, settings=settings,
            guild_id=guild_id, prefer_create=prefer_create)

        created_channel_id = None
        try:
            if plan.mode == "create":
                created = await self._concierge_create_channel(
                    guild_id=guild_id, category_id=category_id,
                    name=_concierge_payload("channel_name_for_blueprint")(blueprint_key))
                if created is None:
                    # Fallback is the plan's own rule: never fail provisioning
                    # over channel creation.
                    if channel_id is None:
                        await self._concierge_respond(
                            interaction, "Couldn't create a channel for it — "
                            "run /bot new inside a channel instead.", ephemeral=ephemeral)
                        return
                    plan = _concierge_payload("ChannelPlan")(
                        "bind", reason="channel creation refused — binding the invoking channel")
                else:
                    channel_id = created
                    created_channel_id = created
            if channel_id is None:
                await self._concierge_respond(
                    interaction, "Couldn't tell which channel you're in — run "
                    "/bot new inside a channel.", ephemeral=ephemeral)
                return

            group = None
            if settings is not None:
                group = resolve_provider_group(
                    settings.provider_groups, guild_id=guild_id,
                    category_id=category_id,
                    default_group=settings.default_provider_group)

            from concierge import provision

            # Off the loop: create_bot does profile-tree creation, a skills
            # copytree, .env copy + adapter discovery, a cron-store write and
            # the registry bind — all under a cross-process flock that can
            # wait up to 60s. Synchronously on the discord.py event loop it
            # freezes ALL gateway event handling (a 60s wait misses the WS
            # heartbeat and forces a reconnect). to_thread is safe here: the
            # per-channel provision lock serializes concurrent callers and
            # create_bot's idempotence re-check happens INSIDE that lock; the
            # result is a plain dict, and every discord.py touch below stays
            # on the loop.
            binding = await asyncio.to_thread(
                provision.create_bot, blueprint_key,
                channel_id=str(channel_id), guild_id=guild_id,
                provider_group=group,
                user_id=str(getattr(getattr(interaction, "user", None), "id", "") or "") or None)
            title = self._concierge_blueprint_title(blueprint_key)
            already = bool(binding.get("already_provisioned"))
            message = (
                f"✅ **{title}** is already live in <#{channel_id}>!"
                if already
                else f"✅ **{title}** is live in <#{channel_id}> — say hi!")
            await self._concierge_respond(interaction, message, ephemeral=ephemeral)
            if created_channel_id is not None:
                # Plan 5.7: the bot says hi IN the channel it now lives in —
                # the ephemeral reply above is visible to the clicker only.
                await self._concierge_greet_created_channel(
                    created_channel_id, title)
            elif (plan.mode == "bind" and not already
                    and self._concierge_channel_is_thread(
                        getattr(interaction, "channel", None))):
                # Forum-post provisioning (a Discord forum post IS a thread):
                # the live 2026-09-27 bug — a BIND into a thread never posted
                # the confirmation INTO the thread, because the greeting
                # branch above is gated on channel creation. Post it through
                # the same best-effort channel-send path, then seed the newly
                # bound bot with the post's opening content. Fresh
                # provisions only (``already``): a re-selection of an
                # existing binding must not re-run the seed turn.
                await self._concierge_greet_thread(interaction, title)
            if source_view is not None:
                try:
                    for child in getattr(source_view, "children", []) or []:
                        child.disabled = True
                except Exception:
                    pass
            logger.info(
                "[%s] concierge: %s bot %r for channel %s (plan=%s)",
                getattr(self, "name", "discord"),
                "returned existing" if already else "provisioned",
                binding.get("profile"), channel_id, plan.mode)
        except Exception as e:
            logger.error(
                "[%s] concierge: /bot new provisioning failed for channel %s: %s",
                getattr(self, "name", "discord"), channel_id, e, exc_info=True)
            await self._concierge_respond(
                interaction,
                "Couldn't set that up — try again in a moment.",
                ephemeral=ephemeral)

    def _concierge_plan_channel(self, *, channel_id, category_id, settings,
                                guild_id=None, prefer_create: bool = False):
        """Task 5.7 decision: create a channel or bind the invoking one. The
        permission half of ``create_vs_bind`` is the MANAGE_CHANNELS
        pre-check on the hub's guild object, fed the REAL ``guild_id`` from
        the interaction/channel context (``None`` here would always take the
        unknowable→True branch and pre-check nothing). Best effort by design:
        a denied create still falls back to bind at run time — the API
        denial remains the enforcement of last resort.

        ``prefer_create=True`` is the ``/bot new`` flow (a fresh channel per
        bot in bot country); the Task 5.5 auto-offer sites pass False (the
        posted-in channel is the user's fresh canvas — bind it)."""
        can_create = self._concierge_can_create_channels(guild_id=guild_id)
        managed = settings.managed_category_ids if settings is not None else []
        return _concierge_payload("create_vs_bind")(
            can_create_channels=can_create,
            category_id=category_id,
            managed_category_ids=managed,
            channel_id=channel_id,
            prefer_create=prefer_create,
        )

    def _concierge_can_create_channels(self, *, guild_id) -> bool:
        """Best-effort MANAGE_CHANNELS check on the hub's guild object; True
        conservatively when unknowable (a denied create falls back to bind —
        failing closed here would silently disable the feature for bots whose
        permissions object isn't cached)."""
        client = getattr(self, "_client", None)
        guild = None
        if client is not None and guild_id is not None:
            try:
                guild = client.get_guild(int(guild_id)) if hasattr(client, "get_guild") else None
            except Exception:
                guild = None
            if guild is None and hasattr(client, "guilds"):
                try:
                    guild = next(
                        (g for g in client.guilds if str(getattr(g, "id", "")) == str(guild_id)),
                        None)
                except Exception:
                    guild = None
        me = getattr(guild, "me", None) if guild is not None else None
        perms = getattr(me, "guild_permissions", None) if me is not None else None
        manage = getattr(perms, "manage_channels", None) if perms is not None else None
        if manage is None:
            return True  # unknowable: let Discord itself referee the create call
        return bool(manage)

    async def _concierge_create_channel(self, *, guild_id, category_id, name):
        """Hub-side ``#<bot-name>`` creation (Task 5.7). Returns the new
        channel id, or ``None`` when creation is impossible/denied — callers
        fall back to binding the invoking channel. Best-effort by design:
        provisioning must never fail over channel creation."""
        client = getattr(self, "_client", None)
        if client is None or guild_id is None:
            return None
        guild = None
        try:
            if hasattr(client, "get_guild"):
                guild = client.get_guild(int(guild_id))
            if guild is None and hasattr(client, "guilds"):
                guild = next(
                    (g for g in client.guilds if str(getattr(g, "id", "")) == str(guild_id)),
                    None)
        except Exception:
            guild = None
        if guild is None:
            return None
        try:
            import discord

            kwargs = {"name": name}
            if category_id is not None:
                # A known category id (the managed one) is passed through;
                # discord.py accepts the raw id for category=.
                category = None
                try:
                    category = client.get_channel(int(category_id))
                except Exception:
                    category = None
                if category is not None:
                    kwargs["category"] = category
            created = await guild.create_text_channel(**kwargs)
            channel_id = str(getattr(created, "id", ""))
            # Suppress the create event's own auto-offer: this channel was
            # created BY the hub (both /room create and /bot new's create
            # plan ride this one helper), and the creating flow posts its
            # own greeting/confirmation into it.
            self._concierge_remember_hub_created_channel(channel_id)
            return channel_id
        except Exception as e:
            logger.warning(
                "[%s] concierge: channel creation failed (%s); falling back to "
                "binding the invoking channel",
                getattr(self, "name", "discord"), e)
            return None

    async def _concierge_greet_created_channel(self, channel_id,
                                               title: str) -> None:
        """Plan 5.7's "bot says hi": a non-ephemeral greeting posted into a
        channel the hub just created — delegated to
        ``_concierge_post_to_channel`` (best effort, never fails the caller;
        see that helper for the posture)."""
        await self._concierge_post_to_channel(
            channel_id, f"✅ **{title}** is live here — say hi!")

    async def _concierge_greet_thread(self, interaction, title: str) -> None:
        """The forum-post greeting (the thread-bind counterpart of
        ``_concierge_greet_created_channel``): post the confirmation INTO
        the thread through the same best-effort send, then seed the newly
        bound bot with the post's opening content.

        Why both ride here: a forum post's body is written at creation —
        BEFORE the binding existed — so it can never arrive as a routed
        message; without an explicit seed the new bot sits silent until the
        user types a second message (the live 2026-09-27 bug). The seed
        reuses the room fan-out's member-turn machinery (fresh source
        stamped with the bound profile, dispatched through the REAL
        ``super().handle_message``) — NO second turn mechanism. Best
        effort, error-isolated: neither a greeting failure nor a seed
        failure may fail the provisioning that already succeeded."""
        channel_id = self._concierge_interaction_channel_id(interaction)
        await self._concierge_post_to_channel(
            channel_id, f"✅ **{title}** is live here — say hi!",
            channel=getattr(interaction, "channel", None))
        await self._concierge_seed_thread_opening(interaction, channel_id)

    async def _concierge_seed_thread_opening(self, interaction,
                                             channel_id) -> None:
        """One seed turn for a freshly thread-bound bot, carrying the post's
        opening message (the starter message the user wrote at post
        creation — content that predates the binding and therefore can
        never reach the bot as a routed message).

        The turn rides the EXISTING dispatch: a source built through the
        REAL ``build_source`` (the concierge routing seam stamps the bound
        profile exactly as for any later thread message — the registry
        binding was just written), a MessageEvent carrying the starter
        content, dispatched via ``handle_message`` — canonicalization,
        profile-namespaced session key, busy guard and the runner's
        profile runtime scope all run as for any bound-channel message,
        and the reply lands in the thread through the same delivery path
        (the faaf55aac2 cross-profile origin-delivery fix). Spawns the same
        way the room fan-out driver does (returns fast, work on a task),
        then waits bounded for the seed turn so provisioning completes.
        Never raises: a seed failure is logged, never a provisioning one."""
        try:
            thread = getattr(interaction, "channel", None)
            if thread is None or channel_id is None:
                return
            # The post's opening content: the starter message the thread
            # carries (discord.py forum threads expose it as
            # ``starter_message``; older surfaces fetch by id). Missing
            # content means there is nothing to seed — the greeting already
            # told the user to say hi.
            content = None
            starter = getattr(thread, "starter_message", None)
            if starter is not None:
                content = getattr(starter, "content", None)
            if not (content or "").strip():
                logger.debug(
                    "[%s] concierge: forum post %s has no opening message "
                    "to seed; the greeting stands alone",
                    getattr(self, "name", "discord"), channel_id)
                return

            from gateway.platforms.event import MessageEvent, MessageType

            channel_name = getattr(thread, "name", "") or ""
            parent = getattr(thread, "parent", None)
            parent_id = (str(getattr(parent, "id", "") or "")
                         if parent is not None else None)
            source = self.build_source(
                chat_id=str(channel_id), chat_name=channel_name,
                chat_type="thread",
                user_id=str(getattr(interaction, "user_id", "")
                            or getattr(getattr(interaction, "user", None),
                                      "id", "") or ""),
                user_name=getattr(getattr(interaction, "user", None),
                                  "display_name", None),
                thread_id=str(channel_id),
                guild_id=self._concierge_interaction_guild_id(interaction),
                parent_chat_id=parent_id,
            )
            event = MessageEvent(
                text=str(content), message_type=MessageType.TEXT,
                source=source, raw_message=thread,
            )
            task = asyncio.create_task(self.handle_message(event))
            background = getattr(self, "_background_tasks", None)
            if background is not None:
                try:
                    background.add(task)
                    task.add_done_callback(background.discard)
                except Exception:
                    pass
            logger.info(
                "[%s] concierge: seeded the forum post %s opening content "
                "into the newly bound bot",
                getattr(self, "name", "discord"), channel_id)
        except Exception as e:
            logger.warning(
                "[%s] concierge: could not seed the forum post's opening "
                "content for channel %s; the bot stays available for the "
                "next message: %s",
                getattr(self, "name", "discord"), channel_id, e)


    async def _concierge_post_to_channel(self, channel_id, text: str,
                                         channel=None) -> None:
        """Best-effort non-ephemeral post into a channel (the created-channel
        greeting, the forum-post greeting and the room greeting share it):
        the destination resolved from an in-hand ``channel`` object when the
        caller holds one (the thread-bind path does — the interaction's
        channel IS the thread), else through the hub's own client. Never
        raises, and a post that cannot be sent is logged and skipped — the
        command that triggered it already succeeded."""
        if channel is None:
            client = getattr(self, "_client", None)
            if client is not None and channel_id is not None \
                    and hasattr(client, "get_channel"):
                try:
                    channel = client.get_channel(int(channel_id))
                except Exception:
                    channel = None
        try:
            if channel is None or not hasattr(channel, "send"):
                logger.warning(
                    "[%s] concierge: channel %s is not reachable for a "
                    "planned post; skipped it (the command itself succeeded)",
                    getattr(self, "name", "discord"), channel_id)
                return
            await channel.send(text)
        except Exception as e:
            logger.warning(
                "[%s] concierge: could not post into channel %s (%s); the "
                "command itself succeeded",
                getattr(self, "name", "discord"), channel_id, e)

    # ── Task 7.4: /room create | add | remove | disband ─────────────────────

    async def _concierge_cmd_room_create(self, interaction,
                                         topic: str | None = None) -> None:
        """``/room create [topic]``: an ephemeral MULTI-SELECT of this guild's
        bound bots (2–6); the selection creates ``#room-<topic>`` and rooms
        it (Task 7.4). Fewer than two guild bots is actionable refusal — no
        picker at all."""
        from concierge import registry

        if not await self._concierge_gate_interaction(interaction, "/room create"):
            return
        guild_id = self._concierge_interaction_guild_id(interaction)
        choices = _concierge_payload("build_room_member_choices")(
            registry.list_bindings(), guild_id)
        if len(choices) < 2:
            await self._concierge_respond(
                interaction,
                "A room needs at least two bots — set more up with "
                "`/bot new` first, then try again.", ephemeral=True)
            return
        view = self._concierge_build_room_picker_view(choices, topic=topic)
        await self._concierge_respond(
            interaction,
            "Pick **2–6 bots** for the room — they'll all answer in the new "
            "channel.", view=view, ephemeral=True)

    def _concierge_build_room_picker_view(self, choices, *, topic=None):
        """The multi-select View for ``/room create``: ONE Select with
        ``min_values=2`` / ``max_values=MAX_ROOM_MEMBERS`` (discord.py
        selects are multi-select when the bounds allow it — the same View
        machinery the /bot picker uses, no modal needed). The topic threads
        into the select callback so the created channel is named after what
        the user asked for at command time."""
        import discord
        from concierge.registry import MAX_ROOM_MEMBERS

        view = discord.ui.View(timeout=300)
        options = [
            discord.SelectOption(
                label=c["label"][:100],
                value=c["value"],
                description=(c.get("description") or "")[:100],
            )
            for c in choices[:25]
        ]
        select = discord.ui.Select(
            placeholder="Pick this room's bots…", options=options,
            min_values=2, max_values=min(MAX_ROOM_MEMBERS, len(options)))
        select.callback = self._concierge_make_room_picker_callback(
            view, topic=topic)
        view.add_item(select)
        return view

    def _concierge_make_room_picker_callback(self, view, *, topic=None):
        """Bound callback for the room picker's multi-select: auth, defer
        (the channel create + registry write are slow), then create the room
        (see ``_concierge_create_room_from_interaction``)."""
        async def _on_select(interaction: discord.Interaction) -> None:
            if not self._concierge_component_auth(interaction):
                await interaction.response.send_message(
                    "You're not allowed to use this.", ephemeral=True)
                return
            await self._concierge_defer(interaction, ephemeral=True)
            values = [str(v) for v in
                      (getattr(interaction, "data", {}) or {}).get("values", [])]
            await self._concierge_create_room_from_interaction(
                interaction, values, topic=topic, source_view=view)

        return _on_select

    async def _concierge_create_room_from_interaction(
        self, interaction, values, *, topic=None, source_view=None,
    ) -> None:
        """The room-creation flow behind the picker: validate the selection
        (pure payload builder — 2–6 of THIS guild's bound bots), create
        ``#room-<topic>`` via the Task 5.7 channel-creation path, register
        the room on the NEW channel, and post the greeting naming the members
        into it.

        Unlike ``/bot new`` there is NO bind-the-invoking-channel fallback:
        a room NEEDS its own channel (plan), so a refused creation is a
        refusal with actionable copy — the invoking channel is never turned
        into a room. The registry mutation is a flock-guarded sync write:
        ``asyncio.to_thread`` (M5c discipline)."""
        from concierge import registry

        guild_id = self._concierge_interaction_guild_id(interaction)
        choices = _concierge_payload("build_room_member_choices")(
            registry.list_bindings(), guild_id)
        error = _concierge_payload("validate_room_selection")(values, choices)
        if error:
            await self._concierge_respond(interaction, error, ephemeral=True)
            return
        members = list(dict.fromkeys(str(v) for v in values))

        settings = self._concierge_settings_obj()
        category_id = self._concierge_room_category_id(interaction, settings)
        created = await self._concierge_create_channel(
            guild_id=guild_id, category_id=category_id,
            name=_concierge_payload("room_channel_name")(topic))
        if created is None:
            # The live 403 path ('Missing Permissions (50013)'): unlike
            # /bot new there is no bind-the-invoking-channel fallback here —
            # a room NEEDS its own channel, so the refusal is EPHEMERAL,
            # actionable, and leaves the registry untouched (no partial room).
            await self._concierge_respond(
                interaction,
                "I couldn't create a channel — grant me the **Manage "
                "Channels** permission (Server Settings → Roles), then run "
                "/room create again.",
                ephemeral=True)
            return
        try:
            # Off the loop: create_room is a flock-guarded sync write.
            await asyncio.to_thread(registry.create_room, created, members)
        except ValueError as e:
            # The registry is the authority (cap/duplicates); its message is
            # already user-appropriate.
            await self._concierge_respond(
                interaction, f"Couldn't create the room — {e}", ephemeral=True)
            return
        titles = [self._concierge_member_title(m) for m in members]
        await self._concierge_respond(
            interaction,
            f"✅ Room created in <#{created}> — "
            + ", ".join(f"**{t}**" for t in titles) + " will answer there.",
            ephemeral=True)
        await self._concierge_post_to_channel(
            created, _concierge_payload("build_room_created_response")(titles))
        if source_view is not None:
            try:
                for child in getattr(source_view, "children", []) or []:
                    child.disabled = True
            except Exception:
                pass
        logger.info(
            "[%s] concierge: created bot room on channel %s (%d members)",
            getattr(self, "name", "discord"), created, len(members))

    def _concierge_room_category_id(self, interaction, settings):
        """The managed category a room channel is created in: the invoking
        channel's category when that one is managed, else the first
        configured managed category, else ``None`` (the guild's top level —
        Discord accepts it and never guesses, same as we do)."""
        managed = [str(c) for c in (
            (settings.managed_category_ids if settings is not None else [])
            or [])]
        invoking = self._concierge_interaction_category_id(interaction)
        if invoking is not None and invoking in managed:
            return invoking
        return managed[0] if managed else None

    def _concierge_room_of_interaction(self, interaction) -> list[str]:
        """The room members of the channel the interaction was invoked IN
        (the /room add|remove|disband operand), or ``[]`` when that channel
        is not a room — callers answer the actionable refusal."""
        from concierge import registry

        channel_id = self._concierge_interaction_channel_id(interaction)
        if not channel_id:
            return []
        return registry.room_members(str(channel_id))

    async def _concierge_cmd_room_add(self, interaction, bot: str) -> None:
        """``/room add <bot>``: a guild-scoped bound bot joins the room of
        the INVOKING channel. Refusals: not-a-room (actionable copy), a bot
        that isn't this guild's; the registry's own ValueError (duplicate,
        full room) surfaces verbatim — it is the authority."""
        from concierge import registry

        if not await self._concierge_gate_interaction(interaction, "/room add"):
            return
        channel_id = self._concierge_interaction_channel_id(interaction)
        members = self._concierge_room_of_interaction(interaction)
        if not members:
            await self._concierge_respond(
                interaction,
                "This channel isn't a bot room — create one with "
                "`/room create` first.", ephemeral=True)
            return
        profile = str(bot or "").strip()
        guild_id = self._concierge_interaction_guild_id(interaction)
        choices = _concierge_payload("build_room_member_choices")(
            registry.list_bindings(), guild_id)
        if not any(c["value"] == profile for c in choices):
            await self._concierge_respond(
                interaction,
                f"`{profile}` isn't one of this server's bots — check "
                f"`/bot list`.", ephemeral=True)
            return
        await self._concierge_defer(interaction, ephemeral=True)
        try:
            # Off the loop: add_to_room is a flock-guarded sync write.
            new_members = await asyncio.to_thread(
                registry.add_to_room, str(channel_id), profile)
        except ValueError as e:
            await self._concierge_respond(
                interaction, f"Couldn't add it — {e}", ephemeral=True)
            return
        await self._concierge_respond(
            interaction,
            f"✅ **{self._concierge_member_title(profile)}** joined the "
            f"room.\n" + _concierge_payload("build_room_info_response")(
                [self._concierge_member_title(m) for m in new_members]),
            ephemeral=True)

    async def _concierge_cmd_room_remove(self, interaction, bot: str) -> None:
        """``/room remove <bot>``: drop a member from the invoking channel's
        room. The registry's own last-removal contract (the room disbands)
        is SURFACED in the reply copy, not hidden."""
        from concierge import registry

        if not await self._concierge_gate_interaction(interaction, "/room remove"):
            return
        channel_id = self._concierge_interaction_channel_id(interaction)
        members = self._concierge_room_of_interaction(interaction)
        if not members:
            await self._concierge_respond(
                interaction,
                "This channel isn't a bot room — create one with "
                "`/room create` first.", ephemeral=True)
            return
        profile = str(bot or "").strip()
        if profile not in members:
            await self._concierge_respond(
                interaction, f"`{profile}` isn't in this room.", ephemeral=True)
            return
        await self._concierge_defer(interaction, ephemeral=True)
        # Off the loop: remove_from_room is a flock-guarded sync write.
        removed = await asyncio.to_thread(
            registry.remove_from_room, str(channel_id), profile)
        members_after = registry.room_members(str(channel_id))
        title = self._concierge_member_title(profile)
        if removed and not members_after:
            await self._concierge_respond(
                interaction,
                f"🗑 Removed **{title}** — that was the last bot, so the "
                f"room is disbanded.", ephemeral=True)
        elif removed:
            await self._concierge_respond(
                interaction,
                f"🗑 Removed **{title}** from the room.\n"
                + _concierge_payload("build_room_info_response")(
                    [self._concierge_member_title(m) for m in members_after]),
                ephemeral=True)
        else:
            await self._concierge_respond(
                interaction, f"`{profile}` isn't in this room.", ephemeral=True)

    async def _concierge_cmd_room_disband(self, interaction) -> None:
        """``/room disband``: a confirmation button (destructive), then the
        registry-driven teardown — every member removed, the last removal
        deletes the room key (remove_from_room's own contract). The channel
        itself stays."""
        from concierge import registry

        if not await self._concierge_gate_interaction(interaction, "/room disband"):
            return
        channel_id = self._concierge_interaction_channel_id(interaction)
        members = self._concierge_room_of_interaction(interaction)
        if not members:
            await self._concierge_respond(
                interaction,
                "This channel isn't a bot room — create one with "
                "`/room create` first.", ephemeral=True)
            return
        titles = [self._concierge_member_title(m) for m in members]
        content = _concierge_payload("build_room_disband_response")(titles)

        import discord

        view = discord.ui.View(timeout=120)

        async def _disband_now(btn_interaction):
            if not self._concierge_component_auth(btn_interaction):
                await btn_interaction.response.send_message(
                    "You're not allowed to use this.", ephemeral=True)
                return
            # Defer BEFORE the teardown: registry writes under the flock.
            await self._concierge_defer(btn_interaction, ephemeral=True)
            try:
                await asyncio.to_thread(
                    self._concierge_disband_room_members, str(channel_id))
            except Exception as e:
                logger.error(
                    "[%s] concierge: disband of the room on channel %s "
                    "failed: %s",
                    getattr(self, "name", "discord"), channel_id, e,
                    exc_info=True)
                await self._concierge_respond(
                    btn_interaction,
                    "Couldn't disband it — try again in a moment.",
                    ephemeral=True)
                return
            try:
                for child in view.children:
                    child.disabled = True
            except Exception:
                pass
            await self._concierge_respond(
                btn_interaction,
                "🗑 Room disbanded — the channel stays; the bots no longer "
                "answer here.", ephemeral=True)

        async def _cancel(btn_interaction):
            try:
                for child in view.children:
                    child.disabled = True
            except Exception:
                pass
            await btn_interaction.response.send_message(
                "Cancelled — the room is untouched.", ephemeral=True)

        disband_btn = discord.ui.Button(
            label="Disband room", style=discord.ButtonStyle.danger)
        disband_btn.callback = _disband_now
        cancel_btn = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_btn.callback = _cancel
        view.add_item(disband_btn)
        view.add_item(cancel_btn)
        await self._concierge_respond(
            interaction, content, view=view, ephemeral=True)

    def _concierge_disband_room_members(self, channel_id: str) -> None:
        """Registry-driven room teardown (runs on a worker thread): remove
        every member; the LAST removal deletes the room key
        (remove_from_room's own contract — no second deletion path)."""
        from concierge import registry

        for member in list(registry.room_members(channel_id)):
            registry.remove_from_room(channel_id, member)

    # ── channel-delete auto-teardown ────────────────────────────────────────

    async def _concierge_on_guild_channel_delete(self, channel) -> None:
        """``on_guild_channel_delete``: the mirror of the create-side
        auto-offer. A deleted channel's registry entry is torn down; a
        single-bot binding whose profile has NO other bound channels is
        retired with ``retire_bot(delete_profile=True)`` — a bot with nowhere
        left to answer is an orphan no retire picker can list. A room channel
        keeps its member bots (each is still bound to its own channel); only
        the room key dies. Never raises: broadcast event, same contract as
        the create-side handler."""
        try:
            if not self._concierge_events_wanted():
                return
            from concierge import registry

            chan_id = getattr(channel, "id", None)
            if chan_id is None:
                return
            cid = str(chan_id)
            binding = registry.get_binding(cid)
            room = registry.room_members(cid)
            if binding is None and not room:
                return

            def _teardown():
                if room:
                    # Members stay bound to their own channels — disband the
                    # room key only.
                    self._concierge_disband_room_members(cid)
                    return None
                profile = str((binding or {}).get("profile", ""))
                if not profile:
                    registry.unbind_channel(cid)
                    return None
                # Retire ONLY when the deleted channel was the bot's last:
                # retire_bot's own order (pause → delete → unbind LAST) is
                # what keeps a mid-teardown crash retryable, so this path
                # never unbinds ahead of it.
                remaining = [c for c in registry.channels_for_profile(profile)
                             if c != cid]
                if remaining:
                    registry.unbind_channel(cid)
                    return None
                from concierge import provision

                provision.retire_bot(profile, delete_profile=True)
                return profile

            profile = await asyncio.to_thread(_teardown)
            if profile:
                logger.info(
                    "[%s] concierge: channel %s deleted — retired+deleted bot "
                    "%s (no bound channels left)",
                    getattr(self, "name", "discord"), cid, profile)
        except Exception:
            logger.warning(
                "[%s] concierge: could not tear down a deleted channel's "
                "registry entry",
                getattr(self, "name", "discord"), exc_info=True)

    # ── /bot list ───────────────────────────────────────────────────────────

    async def _concierge_cmd_list(self, interaction) -> None:
        """Ephemeral list of this guild's bound bots (Task 5.4)."""
        if not await self._concierge_gate_interaction(interaction, "/bot list"):
            return
        # Defer first: bot_status walks every bot's cron store — the same
        # slow-service class as create_bot, small enough to miss the ~3s
        # interaction token on a busy server. The reply then rides the
        # followup branch of ``_concierge_respond``.
        await self._concierge_defer(interaction, ephemeral=True)
        from concierge import provision, registry

        guild_id = self._concierge_interaction_guild_id(interaction)
        # The payload build (pure module, no discord.py objects) carries the
        # bot_status walk — offload the whole build so the cron-store reads
        # stay off the loop; the registry read itself is the accepted M5b
        # fast path and stays inline.
        payload = await asyncio.to_thread(
            _concierge_payload("build_list_payload"),
            registry.list_bindings(), provision.bot_status, guild_id=guild_id)
        text = ("\n".join(payload["lines"]) if not payload["empty"]
                else "No bots here yet — `/bot new` to set one up.")
        await self._concierge_respond(interaction, text, ephemeral=True)

    # ── /bot memory (Task 9.2) ──────────────────────────────────────────────

    async def _concierge_cmd_memory(self, interaction) -> None:
        """Ephemeral report of what the bots' shared Honcho workspace knows
        about the user (Task 9.2) — "what do my bots know about me?".

        The bots share the user's workspace (provisioning copies the hub's
        honcho.json into every memory-enabled bot), so ONE query against the
        HUB's Honcho answers for the whole team. The client construction AND
        the dialectic query are network I/O — the handler defers first (the
        /bot list contract) and runs the whole build off the loop. Any
        failure answers with the failure copy, never a traceback."""
        if not await self._concierge_gate_interaction(interaction, "/bot memory"):
            return
        await self._concierge_defer(interaction, ephemeral=True)
        from concierge import memory_report

        def _build() -> str:
            client = memory_report.memory_report_client(
                user_id=getattr(getattr(interaction, "user", None), "id", None))
            return memory_report.build_memory_report(client)

        try:
            text = await asyncio.to_thread(_build)
        except Exception as e:
            logger.error(
                "[%s] concierge: /bot memory failed: %s",
                getattr(self, "name", "discord"), e, exc_info=True)
            text = memory_report.FAILURE_COPY
        await self._concierge_respond(interaction, text, ephemeral=True)

    # ── /bot retire ─────────────────────────────────────────────────────────

    async def _concierge_cmd_retire(self, interaction, bot: str) -> None:
        """Confirm-then-retire (Task 5.4): a picker of this guild's bots (the
        free-text argument names the bot directly when it matches), then
        ``retire_bot(delete_profile=True)`` — the Discord surface's canonical
        teardown, same as the CLI's ``--delete`` flow."""
        from concierge import registry

        if not await self._concierge_gate_interaction(interaction, "/bot retire"):
            return
        guild_id = self._concierge_interaction_guild_id(interaction)
        bindings = registry.list_bindings()
        choices = _concierge_payload("build_retire_choices")(bindings, guild_id)

        profile = str(bot or "").strip()
        if profile and not any(c["value"] == profile for c in choices):
            # Free text that isn't this guild's bot: refuse — never retire a
            # profile the invoking guild cannot see.
            await self._concierge_respond(
                interaction,
                f"`{profile}` isn't one of this server's bots — check `/bot list`.",
                ephemeral=True)
            return
        if not profile:
            if not choices:
                await self._concierge_respond(
                    interaction, "No bots to retire — `/bot list`.", ephemeral=True)
                return
            profile = choices[0]["value"]
        await self._concierge_confirm_and_retire(interaction, profile, choices)

    async def _concierge_confirm_and_retire(self, interaction, profile: str,
                                            choices) -> None:
        """The confirmation button row: Retire / Cancel. One chance to undo —
        retiring deletes the profile."""
        import discord

        view = discord.ui.View(timeout=120)

        async def _retire_now(btn_interaction):
            if not self._concierge_component_auth(btn_interaction):
                await btn_interaction.response.send_message(
                    "You're not allowed to use this.", ephemeral=True)
                return
            # Defer BEFORE the teardown: retire_bot pauses cron stores and
            # deletes the profile tree — slow enough to burn the ~3s token.
            await self._concierge_defer(btn_interaction, ephemeral=True)
            from concierge import provision

            try:
                # Off the loop, same contract as create_bot above: the
                # teardown is filesystem + cron-store work under the
                # provision lock.
                await asyncio.to_thread(
                    provision.retire_bot, profile, delete_profile=True)
            except Exception as e:
                logger.error(
                    "[%s] concierge: retire of %s failed: %s",
                    getattr(self, "name", "discord"), profile, e, exc_info=True)
                await self._concierge_respond(
                    btn_interaction,
                    "Couldn't retire it — try again in a moment.",
                    ephemeral=True)
                return
            try:
                for child in view.children:
                    child.disabled = True
            except Exception:
                pass
            await self._concierge_respond(
                btn_interaction,
                f"🗑 Retired **{self._concierge_profile_title(profile, choices)}**.",
                ephemeral=True)

        async def _cancel(btn_interaction):
            try:
                for child in view.children:
                    child.disabled = True
            except Exception:
                pass
            await btn_interaction.response.send_message(
                "Cancelled — nothing was retired.", ephemeral=True)

        retire_btn = discord.ui.Button(
            label=f"Retire {self._concierge_profile_title(profile, choices)}",
            style=discord.ButtonStyle.danger)
        retire_btn.callback = _retire_now
        cancel_btn = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_btn.callback = _cancel
        view.add_item(retire_btn)
        view.add_item(cancel_btn)
        await self._concierge_respond(
            interaction,
            f"Retire **{self._concierge_profile_title(profile, choices)}**? "
            "This pauses its routine and deletes its profile.",
            view=view, ephemeral=True)

    # ── Task 8.2: /connect — account-connection deep links ─────────────────

    async def _concierge_cmd_connect(self, interaction,
                                     kind: str | None = None) -> None:
        """/connect [kind]: account-connection status + deep links into the
        gateway dashboard's EXISTING connect pages (no new OAuth plumbing).

        No kind: every account kind this guild's bound bots need, with
        configured/unconfigured status and a deep-link BUTTON per
        unconfigured kind. With a kind: the ephemeral deep link (and
        status) for that kind. Payloads come from the PURE
        ``concierge/connections.py`` (paths discovered in Task 8.1, base
        URL from the dashboard's own public-URL config); the probe is
        CHEAP-LOCAL (see connections.account_probe) so it is safe here just
        as on the message path."""
        if not await self._concierge_gate_interaction(interaction, "/connect"):
            return
        try:
            from concierge import connections, registry

            guild_id = self._concierge_interaction_guild_id(interaction)
            bindings = registry.list_bindings()
            base_url = connections.dashboard_base_url()

            if kind:
                kind = str(kind).strip()
                profile = connections.probe_profile_for(
                    bindings, guild_id, kind)
                configured = (None if profile is None else bool(
                    connections.account_probe(profile)(kind)))
                payload = connections.build_connect_kind_reply(
                    kind, configured, base_url)
                await self._concierge_respond(
                    interaction, payload["content"],
                    view=self._concierge_connect_view([payload["link"]]),
                    ephemeral=True)
                return

            has_bots = any(
                isinstance(b, dict)
                and (not guild_id or b.get("guild_id") in (None, str(guild_id)))
                for b in bindings.values())
            if not has_bots:
                await self._concierge_respond(
                    interaction,
                    "No bots here yet — `/bot new` to set one up.",
                    ephemeral=True)
                return
            needed = connections.needed_kinds(bindings, guild_id)
            if not needed:
                await self._concierge_respond(
                    interaction,
                    "None of this server's bots need account connections.",
                    ephemeral=True)
                return
            configured = {}
            for account_kind in needed:
                profile = connections.probe_profile_for(
                    bindings, guild_id, account_kind)
                if profile is None:
                    # needed_kinds only yields kinds a guild binding
                    # declared, so a None here means the registry shifted
                    # under us mid-command: fail OPEN (never claim the
                    # user's account is missing over a race we caused).
                    configured[account_kind] = True
                    continue
                configured[account_kind] = bool(
                    connections.account_probe(profile)(account_kind))
            payload = connections.build_connect_status(
                bindings, guild_id, configured, base_url)
            text = ("Accounts this server's bots depend on:\n"
                    + "\n".join(payload["lines"]))
            await self._concierge_respond(
                interaction, text,
                view=self._concierge_connect_view(payload["links"]),
                ephemeral=True)
        except Exception as e:
            logger.error(
                "[%s] concierge: /connect failed: %s",
                getattr(self, "name", "discord"), e, exc_info=True)
            await self._concierge_respond(
                interaction,
                "Couldn't check that right now — try again in a moment.",
                ephemeral=True)

    def _concierge_connect_view(self, links):
        """Deep-link BUTTONS for the payload's link entries (discord.py
        URL buttons — ``url=`` makes them link buttons; no callback, no
        state). ``None`` when there is nothing to tap (no dashboard base
        URL configured, or every needed account is already connected)."""
        import discord

        entries = [link for link in (links or [])
                    if isinstance(link, dict) and link.get("url")]
        if not entries:
            return None
        view = discord.ui.View(timeout=300)
        for link in entries:
            view.add_item(discord.ui.Button(
                label=f"Connect {link.get('label', 'account')}",
                url=link["url"]))
        return view

    # ── shared plumbing ──────────────────────────────────────────────────────

    async def _concierge_gate_interaction(self, interaction, command_text) -> bool:
        """The adapter's own slash authorization gate, when present (the mixin
        rides the adapter class, so it normally is); fail-closed otherwise."""
        gate = getattr(self, "_check_slash_authorization", None)
        if callable(gate):
            return bool(await gate(interaction, command_text))
        return False

    async def _concierge_defer(self, interaction, *, ephemeral: bool = True) -> None:
        """``defer()`` before slow work (the adapter's own slash paths do the
        same — see ``_run_simple_slash``: auth gate FIRST so the ephemeral
        rejection can still be sent, then defer, then the work, then the
        followup). An interaction token must be answered within ~3s; the
        provisioning/retire teardown (profile copytree, cron-store writes, a
        cross-process flock that can wait up to 60s) can outlast it, and an
        unanswered token burns even the failure copy. After a successful
        defer ``_concierge_respond`` takes its followup branch, so every
        outcome stays deliverable. Never raises — a defer that fails (token
        already gone, response already done) just means the eventual reply
        follows up or dies quietly, the same posture as
        ``_concierge_respond`` itself."""
        try:
            await interaction.response.defer(ephemeral=ephemeral)
        except Exception as e:
            logger.debug("[%s] concierge: interaction defer failed: %s",
                         getattr(self, "name", "discord"), e)

    async def _concierge_respond(self, interaction, text: str, *,
                                 view=None, ephemeral: bool = True) -> None:
        """Respond or follow up — whichever the interaction still allows
        (picker selections arrive as component interactions that may already
        be deferred). Never raises: command UX must not die on a stale token."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, view=view, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(text, view=view, ephemeral=ephemeral)
        except Exception as e:
            logger.debug("[%s] concierge: interaction response failed: %s",
                         getattr(self, "name", "discord"), e)

    def _concierge_blueprint_title(self, blueprint_key: str) -> str:
        try:
            from concierge.blueprints import get_blueprint

            return get_blueprint(blueprint_key).title
        except KeyError:
            return blueprint_key

    def _concierge_profile_title(self, profile: str, choices) -> str:
        for c in choices or []:
            if c.get("value") == profile:
                return c.get("label") or profile
        return profile

    def _concierge_interaction_channel_id(self, interaction):
        chan = getattr(interaction, "channel", None)
        raw = getattr(interaction, "channel_id", None) or getattr(chan, "id", None)
        return str(raw) if raw is not None else None

    def _concierge_interaction_guild_id(self, interaction):
        raw = getattr(interaction, "guild_id", None) or getattr(
            getattr(interaction, "guild", None), "id", None)
        return str(raw) if raw is not None else None

    def _concierge_interaction_category_id(self, interaction):
        chan = getattr(interaction, "channel", None)
        raw = getattr(chan, "category_id", None) or getattr(
            getattr(chan, "category", None), "id", None)
        return str(raw) if raw is not None else None

    # ── Task 5.5: channel-create / forum-post auto-offer ────────────────────

    def _concierge_remember_hub_created_channel(self, channel_id) -> None:
        """Record *channel_id* as JUST created by the hub (see
        ``_HUB_CREATED_CHANNEL_TTL_SECONDS``): the create event's own
        auto-offer is suppressed for it. Lazy state — the mixin must not
        require an ``__init__``. Never raises: a failed remember only risks
        one redundant picker."""
        try:
            cid = str(channel_id or "")
            if not cid:
                return
            seen = getattr(self, "_concierge_hub_created_channels", None)
            if seen is None:
                seen = self._concierge_hub_created_channels = {}
            seen[cid] = time.monotonic() + _HUB_CREATED_CHANNEL_TTL_SECONDS
        except Exception:
            pass

    def _concierge_channel_is_hub_created(self, channel_id) -> bool:
        """True while *channel_id* is inside the hub-created TTL window (an
        expired entry is pruned here and stops suppressing). Never raises."""
        try:
            seen = getattr(self, "_concierge_hub_created_channels", None) or {}
            cid = str(channel_id or "")
            deadline = seen.get(cid)
            if deadline is None:
                return False
            if time.monotonic() > deadline:
                seen.pop(cid, None)
                return False
            return True
        except Exception:
            return False

    async def _concierge_on_guild_channel_create(self, channel) -> None:
        """``on_guild_channel_create``: a new channel in a managed category
        gets the blueprint picker posted into it (non-ephemeral — anyone in
        the channel can click). Never raises: this is a broadcast event, and a
        picker failure must not affect anything else listening.

        FORUM channels are skipped silently (DEBUG only): Discord rejects
        plain messages in a forum channel (only posts/threads carry
        messages), so a picker send would 400 — the noise the 2026-09-27
        live E2E surfaced. A forum channel's OFFERS are owned by the
        thread-create flow (``_concierge_on_thread_create`` fires for each
        new forum post), so skipping here loses nothing. Detection is the
        adapter's own best-effort ``_is_forum_parent`` (isinstance
        discord.ForumChannel, else channel type 15); False when unknowable
        keeps the existing text-channel auto-offer byte-identical."""
        try:
            if not self._concierge_events_wanted():
                return
            if self._concierge_channel_is_forum(channel):
                logger.debug(
                    "[%s] concierge: new channel %s is a forum channel; "
                    "skipping the bot picker (forum posts get their own "
                    "offer via the thread-create flow)",
                    getattr(self, "name", "discord"),
                    getattr(channel, "id", "?"))
                return
            settings = self._concierge_settings_obj()
            category_id = getattr(channel, "category_id", None) or getattr(
                getattr(channel, "category", None), "id", None)
            from concierge.routing import should_auto_offer

            if not should_auto_offer(category_id, settings):
                return
            chan_id = getattr(channel, "id", None)
            if chan_id is None:
                return
            if self._concierge_channel_is_hub_created(chan_id):
                logger.debug(
                    "[%s] concierge: new channel %s was created by the hub; "
                    "skipping the bot picker (the creating flow posts its "
                    "own greeting into it)",
                    getattr(self, "name", "discord"), chan_id)
                return
            payload = _concierge_payload("build_new_bot_response")()
            # The posted-in channel IS the destination: bind it, and let the
            # confirmation land publicly in it (Task 5.5 + the M5c fix).
            view = self._concierge_build_picker_view(
                payload["choices"], prefer_create=False, ephemeral=False)
            await channel.send(payload["content"], view=view)
            logger.info(
                "[%s] concierge: auto-offered bots in new channel %s "
                "(category %s)", getattr(self, "name", "discord"), chan_id,
                category_id)
        except Exception:
            logger.warning(
                "[%s] concierge: could not post the bot picker in a new channel",
                getattr(self, "name", "discord"), exc_info=True)

    async def _concierge_on_thread_create(self, thread) -> None:
        """The Task 5.5 chaining wrapper for ``on_thread_create`` (a forum post
        is a thread): run the concierge auto-offer FIRST — never blocking the
        existing normalizer on it — then hand the thread to the adapter's
        ``_on_platform_thread_create`` unchanged (spike rule: discord.py keeps
        ONE handler per event name; a second ``@client.event`` would REPLACE
        the normalizer, so the adapter's registration block calls this)."""
        try:
            if self._concierge_events_wanted():
                settings = self._concierge_settings_obj()
                parent = getattr(thread, "parent", None)
                category_id = getattr(parent, "category_id", None) or getattr(
                    getattr(parent, "category", None), "id", None)
                from concierge.routing import should_auto_offer

                if should_auto_offer(category_id, settings):
                    chan = thread if hasattr(thread, "send") else parent
                    if chan is not None:
                        payload = _concierge_payload("build_new_bot_response")()
                        # Same rule as on_guild_channel_create: the posted-in
                        # thread IS the destination (bind, public reply).
                        view = self._concierge_build_picker_view(
                            payload["choices"], prefer_create=False,
                            ephemeral=False)
                        await chan.send(payload["content"], view=view)
                        logger.info(
                            "[%s] concierge: auto-offered bots in new forum "
                            "post %s (parent category %s)",
                            getattr(self, "name", "discord"),
                            getattr(thread, "id", "?"), category_id)
        except Exception:
            logger.warning(
                "[%s] concierge: could not post the bot picker in a new thread",
                getattr(self, "name", "discord"), exc_info=True)
        normalizer = getattr(self, "_on_platform_thread_create", None)
        if callable(normalizer):
            await normalizer(thread)


def _concierge_payload(builder: str):
    """Lazy import of a PURE builder from concierge/discord_payloads.py, named
    by string so the mixin module stays importable without the concierge
    package present (a broken install must not break adapter import)."""
    from concierge import discord_payloads

    return getattr(discord_payloads, builder)

