"""Concierge voice routing for the Discord adapter (concierge_voice sibling).

The REGISTRY-VERDICT machinery of the concierge routing, extracted from
``concierge_mixin.py`` (the file-size ratchet): the stamping tail every
concierge-routed source gets (``_concierge_apply_registry_verdict``), the
voice-ingress restamp hook (``_restate_voice_routing_stamps`` — the
duck-typed hook ``gateway/run_voice.py`` calls after ``clear_identity``),
the registry readers that feed them, and the SPOKEN-SUMMARY RELAY
(``concierge.voice_relay``): with ``mode: summary``, a member lane's
answer under a spoken room trigger is synthesized as a short
auxiliary-LLM summary instead of the full text — substituted at the base
auto-TTS seam the Task 2 lane-delivery suite pins, so the ONE-playback
contract holds by construction and the full text still lands in the room.
Typed ingress keeps its entry
point (``build_source``) in the mixin; this sibling owns what both paths
share.

Mount: ``ConciergeVoiceMixin`` joins the DiscordAdapter bases next to
``ConciergeDiscordMixin`` (duck-typed companions: each declares the
helpers the other provides as ``Any``-typed names, and the MRO resolves
them at call time).
These methods only call ``self`` helpers the
companion mixin provides (``_concierge_warn_once``,
``_concierge_stamp_connect_nudge``, ``_concierge_wire_dynamic_router``),
so the two mixins mount together. The stamps stay wire-invisible,
process-local plain attrs a peer cannot forge — the constants live in
``concierge_mixin.py`` where the consuming wrap reads them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from plugins.platforms.discord.concierge_mixin import (
    _CONCIERGE_ORCHESTRATOR_ATTR,
    _ROOM_TARGETS_ATTR,
    _VOICE_RELAY_ATTR,
    _concierge_enabled_from_config,
)

logger = logging.getLogger(__name__)

# ── The spoken-summary relay (concierge.voice_relay, Task 3) ─────────────
#
# Defaults resolve to mode "off" (zero behavior change) and max_chars 400.
# The section rides the config.yaml→GatewayConfig bridge like every other
# ``concierge:`` key (the launch home's config, runbook §2.6) — read from
# the runner's IN-HAND config, never a config-file load on the reply path.

_VOICE_RELAY_TASK = "voice_relay"
_VOICE_RELAY_DEFAULT_MAX_CHARS = 400

# The aux call is pinned per task (``auxiliary.voice_relay`` in the same
# launch-home config; unset = the machinery's auto route). The request
# budget rides the machinery too: no explicit timeout means
# ``auxiliary.voice_relay.timeout`` if set, else the machinery's 30s
# default — a driving user needs the summary in seconds, not a reasoning
# tour.
_VOICE_RELAY_MAX_TOKENS = 200

_VOICE_RELAY_SYSTEM_PROMPT = (
    "You summarize a conversational assistant's answer for someone listening "
    "in a car, hands-free. Reply with 1 to 3 short sentences of plain spoken "
    "text: no markdown, no emojis, no lists, no quotes, no preamble. Keep the "
    "answer's key facts and anything time-sensitive (times, counts, "
    "deadlines). Reply with the summary only."
)


@dataclass(frozen=True)
class _VoiceRelaySettings:
    """The resolved ``concierge.voice_relay`` block; the defaults are the
    feature-off posture (verbatim synthesis, no aux call)."""

    mode: str = "off"
    max_chars: int = _VOICE_RELAY_DEFAULT_MAX_CHARS


def _voice_relay_settings(runner) -> _VoiceRelaySettings:
    """Read the relay settings from the runner's in-hand ``concierge``
    section (the launch home's config — the same placement rule as
    ``_concierge_enabled_from_config``). Fail-closed: an absent block, a
    non-dict block, an unknown mode, or a garbage ``max_chars`` all
    resolve to a no-op posture, never to a crash or a surprise summary."""
    section = getattr(getattr(runner, "config", None), "concierge", None)
    relay = section.get("voice_relay") if isinstance(section, dict) else None
    if not isinstance(relay, dict):
        return _VoiceRelaySettings()
    mode = str(relay.get("mode") or "").strip().lower()
    if mode not in ("summary", "verbatim"):
        return _VoiceRelaySettings()  # off / absent / unknown: no relay work
    try:
        max_chars = int(relay.get("max_chars") or 0)
    except (TypeError, ValueError):
        max_chars = 0
    if max_chars <= 0:
        # Non-positive and garbage caps all resolve to the default: a
        # typo'd ``max_chars: -5`` must fail closed to the documented 400,
        # never silently become summarize-everything via a clamp to 1.
        max_chars = _VOICE_RELAY_DEFAULT_MAX_CHARS
    return _VoiceRelaySettings(mode=mode, max_chars=max_chars)


def _profile_exists(profile: str) -> tuple[bool, str | None]:
    """Live (non-tombstoned) profile check — the same primitive
    ``profile_exists`` gives ``_profile_name_for_source``'s served-set gate,
    without the O(profiles) ``profiles_to_serve`` scan per inbound event.

    Returns ``(exists, stat_error)``. ``stat_error`` is non-None when the check
    itself FAILED (OSError: EACCES, EIO, a stalled mount) — a different incident
    from a cleanly-absent profile, and the caller must say so instead of
    recommending "re-provision". Invalid names (path traversal, reserved ids in
    a hand-edited registry) are NOT I/O failures: ``normalize_profile_name``
    raises ValueError inside ``profile_exists`` and the answer is simply
    "does not exist" — the same verdict ``_profile_name_for_source``'s
    served-set gate would give.

    The failure verdict needs an explicit ``os.stat`` probe:
    ``profile_exists`` checks liveness with ``Path.is_dir()``/``is_file()``,
    which swallow OSError (EACCES reads as "absent" on 3.12+), so without the
    probe the stat_error branch — and the operator-facing
    "could not be checked" diagnosis — could never fire.
    """
    import os

    try:
        from hermes_cli.profiles import get_profile_dir, profile_exists

        try:
            profile_dir = get_profile_dir(profile)  # ValueError → invalid name
        except ValueError:
            return False, None
        try:
            os.stat(profile_dir)
        except FileNotFoundError:
            return False, None  # clean absence: the retired-bot diagnosis
        except OSError as exc:
            return False, repr(exc)  # the check failed: EACCES, EIO, ...
        return profile_exists(profile), None
    except OSError as exc:
        return False, repr(exc)
    except Exception:
        return False, None


class ConciergeVoiceMixin:
    """The registry-verdict half of the concierge routing: what
    ``build_source`` and the voice-ingress restamp share, plus the
    spoken-summary relay at the base auto-TTS seam."""

    # Duck-typed companion helpers, provided by ConciergeDiscordMixin ahead in
    # the adapter MRO (each mixin declares the names the other provides as
    # ``Any``-typed stubs; the MRO resolves them at call time):
    _concierge_warn_once: Any
    _concierge_stamp_connect_nudge: Any
    _concierge_wire_dynamic_router: Any
    # Duck-typed adapter names the home-session lane and the auto-TTS gate
    # read (DiscordVoiceInfoMixin / the base adapter provide them).
    _voice_text_channels: Any
    is_in_voice_channel: Any

    # The relay handoff slot (Task 3): the member-lane voice event whose
    # auto-TTS synthesis follows. Never read across turns — see
    # ``_wants_auto_tts`` below.
    _concierge_relay_tts_event: Any = None

    def _concierge_apply_registry_verdict(self, source, reg) -> None:
        """The registry-verdict tail every concierge-routed source gets:
        the room fan-out stamp, else the single-bot re-home, else nothing
        (an unbound channel keeps the hub answer). Shared by ``build_source``
        and the voice-ingress restamp (``_restate_voice_routing_stamps``) so
        a restated source and a fresh ``build_source`` output carry IDENTICAL
        stamps from the SAME live-registry read. Mutates *source* in place.
        """
        # Task 7.3 call order: the ROOM lookup runs FIRST — a channel is
        # a room XOR a binding (registry invariant), so at most one can
        # match; an empty room verdict falls through to the single-bot
        # binding lookup below.
        room_targets = self._concierge_room_targets_from_registry(source, reg)
        if room_targets:
            setattr(source, _ROOM_TARGETS_ATTR, tuple(room_targets))
            return
        # A fresh not-a-room verdict clears any room stamp an earlier verdict
        # left on this source: a stale stamp would outrank the fresh verdict
        # and keep fanning the utterance out to disbanded members. Clearing is
        # safe at both call sites — the wrap's consumer treats an absent stamp
        # exactly like a never-set one (``_concierge_room_targets_on``).
        if getattr(source, _ROOM_TARGETS_ATTR, None) is not None:
            delattr(source, _ROOM_TARGETS_ATTR)
        profile = self._concierge_profile_from_registry(source, reg)
        if profile is None:
            # Orchestrator redesign (design §4): no binding/room verdict — but
            # the author may have their OWN orchestrator. The re-home needs
            # the channel's category, which lives on the EVENT, not the
            # source, so the verdict tail only STAMPS the process-local
            # orchestrator attr; the set_message_handler wrap consumes it.
            self._concierge_stamp_orchestrator_fallback(source, reg)
            return  # unbound channel: the hub answers until the wrap re-homes
        exists, stat_error = _profile_exists(profile)
        if stat_error is not None:
            # The check FAILED (EACCES, EIO, stalled mount) — not "retired
            # bot". Routing fail-closed either way, but the operator action
            # is "fix the filesystem/gateway user", never "re-provision or
            # unbind", which would destroy a healthy binding over a transient
            # or environmental fault.
            self._concierge_warn_once(
                ("stat", str(source.chat_id), profile),
                "[%s] Concierge binding for channel %s points at profile %r "
                "which could not be checked (%s); routing to the hub instead "
                "— investigate the profile directory/permissions before "
                "re-provisioning or unbinding",
                getattr(self, "name", "discord"), source.chat_id, profile,
                stat_error)
            return
        if not exists:
            self._concierge_warn_once(
                ("ghost", str(source.chat_id), profile),
                "[%s] Concierge binding for channel %s points at profile %r "
                "which does not exist (bot retired?); routing to the hub "
                "instead — re-provision or unbind the channel",
                getattr(self, "name", "discord"), source.chat_id, profile)
            return
        source.profile = profile
        logger.debug(
            "[%s] Concierge: channel %s routed to bot profile %s",
            getattr(self, "name", "discord"), source.chat_id, profile)
        # Task 8.2: the nudge decision rides the SAME registry read the
        # routing verdict came from — the binding is already in hand, so no
        # second registry read and (for connectless blueprints) no probe.
        self._concierge_stamp_connect_nudge(source, reg, profile)

    def _concierge_stamp_orchestrator_fallback(self, source, reg) -> None:
        """Stamp the author's orchestrator profile on *source* (the
        process-local ``_CONCIERGE_ORCHESTRATOR_ATTR`` marker, the
        ``_ROOM_TARGETS_ATTR`` discipline: wire-invisible, never serialized)
        when the registry's USERS section gives the author a LIVE
        orchestrator and no binding/room verdict claimed the channel (this
        tail only runs on the unbound path — a bound or room channel never
        sees it). The WRAP consumes the stamp: it re-homes
        ``source.profile`` before delegation when the channel sits in a
        managed category, and admission confirms via the users section
        (branch (c) of ``_concierge_dynamic_runtime_profile``).

        Anti-loop and fail-closed, the binding path's exact posture:
        bot-authored sources never get the stamp (a bot's own message must
        not route to the orchestrator), and a dead or uncheckable
        orchestrator warns ONCE and leaves the source unstamped — today's
        hub behavior, unchanged."""
        if getattr(source, "is_bot", False):
            return
        users = reg.get("users")
        if not isinstance(users, dict):
            return
        user_id = str(getattr(source, "user_id", "") or "")
        if not user_id:
            return
        entry = users.get(user_id)
        orchestrator = (entry.get("orchestrator")
                        if isinstance(entry, dict) else None)
        if not isinstance(orchestrator, str) or not orchestrator.strip():
            return
        exists, stat_error = _profile_exists(orchestrator)
        if stat_error is not None:
            self._concierge_warn_once(
                ("orchestrator-stat", user_id, orchestrator),
                "[%s] Concierge orchestrator for user %s (profile %r) could "
                "not be checked (%s); leaving the channel on the hub — "
                "investigate the profile directory/permissions before "
                "re-provisioning",
                getattr(self, "name", "discord"), user_id, orchestrator,
                stat_error)
            return
        if not exists:
            self._concierge_warn_once(
                ("orchestrator-ghost", user_id, orchestrator),
                "[%s] Concierge orchestrator for user %s points at profile "
                "%r which does not exist (bot retired?); routing to the hub "
                "instead — re-provision or repair the users entry",
                getattr(self, "name", "discord"), user_id, orchestrator)
            return
        # Card J (§6): a recorded home category Discord deleted (or that
        # cannot be checked) degrades quietly, warn ONCE per user. Reuse THIS
        # registry read (no second load); never create a category, never
        # bootstrap a second orchestrator. Fail-closed: a reconciliation
        # error must never break the orchestrator stamp / routing.
        try:
            self._concierge_resolve_home_category(user_id, reg=reg)
        except Exception:
            logger.debug(
                "[%s] Concierge: home-category reconciliation failed; "
                "routing proceeds unchanged",
                getattr(self, "name", "discord"), exc_info=True)
        setattr(source, _CONCIERGE_ORCHESTRATOR_ATTR, orchestrator)
        logger.debug(
            "[%s] Concierge: unbound channel %s stamped for the author's "
            "orchestrator %s (consumed by the inbound wrap)",
            getattr(self, "name", "discord"),
            getattr(source, "chat_id", "?"), orchestrator)

    def _restate_voice_routing_stamps(self, source) -> None:
        """Restate the concierge routing verdicts on a voice-ingress source.

        ``/voice join`` stores the bound text channel's source SERIALIZED
        (``to_dict``), and the verdicts this mixin stamps ride wire-invisible
        plain attrs (a peer cannot forge them) while the binding profile is
        ``clear_identity``'s to drop — so the copy a spoken utterance rebuilds
        lost exactly the routing a typed message in that channel gets FRESH
        from ``build_source`` on every message. The voice runner calls this
        duck-typed hook after ``clear_identity`` (the registry, not the join
        time, decides — the same no-restart live-mutability a typed message
        gets). Same gates as ``build_source``; never raises: a failure leaves
        the source unstamped and the utterance takes the normal delegation
        path (fail-closed, intake never breaks).

        Speaker→orchestrator lane (design: voice-home-session-rekey): the
        normal registry verdict runs FIRST — a binding or room verdict
        claims the channel exactly as for typed input (a user speaking in a
        worker's channel is talking to that worker; binding routing
        untouched) — and the lane AFTER it consumes the verdict tail's
        EXISTING ``_CONCIERGE_ORCHESTRATOR_ATTR`` stamp: spoken input from
        a user with a live orchestrator runs as their orchestrator ON THEIR
        HOME-CHANNEL SESSION (operator decision 2026-11-17: spoken turns
        continue the same conversation as typed turns in the home channel —
        not a fresh session keyed to the voice-bound text channel), by
        re-keying the source to the recorded home channel BEFORE the
        identity pins (run_voice canonicalizes after this hook), so
        admission confirms via the home channel's BINDING (branch (a) of
        ``_concierge_dynamic_runtime_profile``), by construction. No
        category check for the voice lane (controller decision: spoken
        input is user-scoped, not channel-scoped). Relay rationale: the
        orchestrator's own final turn already contains the
        dispatched-and-summarized worker results, so the existing relay at
        the base auto-TTS seam speaks the right answer — no relay changes.
        """
        runner = getattr(self, "gateway_runner", None)
        if not _concierge_enabled_from_config(runner):
            return
        if not getattr(getattr(runner, "config", None), "multiplex_profiles", False):
            return
        if getattr(source, "profile_route_rejected", False) is True:
            return
        stamped = getattr(source, "profile", None)
        if stamped and stamped != getattr(self, "_owner_profile", None):
            return  # an explicit config route stays authoritative over the registry
        # The restated profile stamp must resolve through admission, the same
        # belt-and-suspenders wiring build_source does (idempotent).
        self._concierge_wire_dynamic_router(runner)
        reg = self._concierge_registry_or_none(source)
        if reg is not None:
            # The normal registry verdict FIRST: a binding or room verdict
            # claims the channel exactly as for typed input, and only its
            # no-verdict tail stamps the orchestrator — the lane below
            # consumes that stamp, never a second users-section read or
            # liveness pass in the stamping itself.
            self._concierge_apply_registry_verdict(source, reg)
            self._concierge_voice_orchestrator_lane(source, reg)

    def _concierge_voice_orchestrator_lane(self, source, reg) -> None:
        """The speaker→orchestrator lane of the voice restamp (design:
        voice-home-session-rekey): consume the verdict tail's EXISTING
        ``_CONCIERGE_ORCHESTRATOR_ATTR`` stamp — placed by
        ``_concierge_apply_registry_verdict`` exactly when NO binding/room
        verdict claimed the channel AND the author has a LIVE orchestrator
        (the stamp tail's own liveness posture and warn keys) — and run the
        spoken turn as the speaker's orchestrator ON THEIR HOME-CHANNEL
        SESSION: the turn continues the same conversation typed turns in
        the home channel run (the operator decision; per-speaker by
        construction, so a shared voice channel works multi-user).

        Admission reconciles BY CONSTRUCTION, now via the home BINDING:
        the lane re-keys the source to the OWNING user's recorded home
        channel (registry accessor, never derived from the trigger event)
        and re-runs the registry verdict on the re-keyed shape, so the
        restamped source carries the IDENTICAL stamps a fresh
        ``build_source`` for the home channel gets (profile re-homed via
        the binding branch, connect-nudge stamp included, orchestrator
        stamp cleared) and ``_concierge_dynamic_runtime_profile`` confirms
        via branch (a) — the home channel is bound to exactly the
        orchestrator by provisioning, a shape the gate below verified
        against the same registry read. Bound and room channels keep their
        existing routing (no stamp ever lands there).

        Fail-closed: no recorded home, the home channel not bound to
        exactly the orchestrator, a room home, or a guild mismatch →
        today's behaviour stands (the re-home on the bound-channel session
        the stamp implies). Bot-authored utterances never take the lane
        (the stamp tail's anti-loop guard, mirrored here). NEVER raises —
        a failure inside the lane leaves the routing as the verdict left
        it; intake never breaks.
        """
        try:
            if getattr(source, "is_bot", False):
                return  # a bot's own utterance never claims a lane
            orchestrator = getattr(source, _CONCIERGE_ORCHESTRATOR_ATTR, None)
            if not isinstance(orchestrator, str) or not orchestrator.strip():
                # No stamp: a binding/room verdict claimed the channel, or
                # the author has no LIVE orchestrator (the stamp tail already
                # warned once for the dead/uncheckable cases) — today's
                # routing stands.
                return
            home = self._concierge_voice_home_rekey_target(
                source, reg, orchestrator)
            if home is None:
                # No recorded home / shape drift: today's re-home stands —
                # the stamp stays for the wrap's idempotent fallback.
                source.profile = orchestrator
                logger.debug(
                    "[%s] Concierge: spoken input from user %s routed to "
                    "their orchestrator %s (personal voice lane, unbound "
                    "channel, no re-keyable home)",
                    getattr(self, "name", "discord"),
                    str(getattr(source, "user_id", "") or "?"), orchestrator)
                return
            self._concierge_voice_rekey_to_home(
                source, reg, home, orchestrator)
        except Exception:
            # Fail-closed, the hook's own contract: a failure inside the
            # lane leaves the source as the verdict left it — the wrap's
            # orchestrator fallback can still re-home (managed categories),
            # else the hub answers. Intake never breaks.
            logger.debug(
                "[%s] Concierge: speaker-to-orchestrator voice re-home "
                "failed; leaving the verdict's routing in place",
                getattr(self, "name", "discord"), exc_info=True)

    def _concierge_voice_home_rekey_target(self, source, reg, orchestrator):
        """The home channel id to re-key *source* onto, or ``None``
        (fail-closed: today's bound-channel re-home stands).

        The home id is the OWNING user's RECORDED one — the registry's
        canonical users-section accessor (``registry.get_user``, tolerant
        of hand-edited files; it re-reads the registry, once per SPOKEN
        utterance, not per message) — never derived from the trigger event.
        The shape gates ride the ONE registry read the verdict already paid:
        the home channel must be BOUND to exactly the stamped orchestrator
        (what admission's branch (a) confirms and the verdict re-run needs)
        and be no room (a room home is a fan-out shape, never a session),
        and the user's recorded guild, when present, must be the voice
        guild — otherwise the re-key would move the reply across guilds.
        """
        user_id = str(getattr(source, "user_id", "") or "")
        voice_guild = str(getattr(source, "guild_id", "") or "")
        if not user_id or not voice_guild:
            return None  # no speaker or no voice guild to key playback to
        try:
            from concierge import registry

            entry = registry.get_user(user_id)
        except Exception:
            logger.debug(
                "[%s] Concierge: reading the speaker's users entry failed; "
                "the spoken turn keeps the bound-channel session",
                getattr(self, "name", "discord"), exc_info=True)
            return None
        if not isinstance(entry, dict):
            return None
        home = entry.get("home_channel")
        if not isinstance(home, str) or not home.strip():
            return None  # no home recorded: today's behaviour stands
        entry_guild = entry.get("guild_id")
        if (isinstance(entry_guild, str) and entry_guild.strip()
                and entry_guild != voice_guild):
            return None  # a home in another guild: channel-shape mismatch
        rooms = reg.get("rooms")
        if isinstance(rooms, dict) and isinstance(rooms.get(home), dict):
            return None  # a room home would fan out, never a 1:1 session
        from concierge.routing import resolve_target_profile

        verdict = resolve_target_profile(
            reg.get("bindings") or {}, channel_id=home, author_is_bot=False)
        if not verdict.routed or verdict.profile != orchestrator:
            return None  # drift: home unbound, or bound to another profile
        return home

    def _concierge_voice_rekey_to_home(self, source, reg, home,
                                       orchestrator) -> None:
        """Re-key *source* onto the home-channel session and restamp it as
        a fresh ``build_source`` for the home channel would be stamped.

        Playback lane FIRST: the re-keyed ``chat_id`` no longer names the
        voice-bound text channel, so the voice seams (auto-TTS gate,
        ``play_tts``, the runner's verbal-ack guild lookup) resolve the
        VOICE GUILD through the lane map this registers. The session-bearing
        shape then matches what a typed message in the home channel carries
        (``build_session_key`` reads exactly these fields; the speaker's
        ``user_id`` stays, preserving per-user isolation), the channel
        labels follow (the session prompt renders them — a stale
        bound-channel label would flip the home session's prompt every
        modality switch), and the verdict re-run leaves the binding
        branch's fresh stamps (profile re-home + connect nudge) with the
        orchestrator stamp CLEARED — identical to fresh home input.
        """
        try:
            self._concierge_voice_home_lane_register(home, source.guild_id)
        except Exception:
            logger.debug(
                "[%s] Concierge: registering the voice home lane failed; "
                "playback falls back to the bound-channel seams",
                getattr(self, "name", "discord"), exc_info=True)
        # The stamp's validated re-home first, kept even if the verdict
        # re-run below declines (idempotent either way).
        source.profile = orchestrator
        source.chat_id = home
        source.chat_type = "group"
        source.thread_id = None
        source.parent_chat_id = None
        source.chat_name, source.chat_topic = self._concierge_voice_home_labels(
            home, orchestrator)
        self._concierge_apply_registry_verdict(source, reg)
        try:
            delattr(source, _CONCIERGE_ORCHESTRATOR_ATTR)
        except AttributeError:
            pass
        logger.debug(
            "[%s] Concierge: spoken input from user %s re-keyed onto their "
            "home channel %s (orchestrator %s)",
            getattr(self, "name", "discord"),
            str(getattr(source, "user_id", "") or "?"), home, orchestrator)

    def _concierge_voice_home_labels(self, home, orchestrator):
        """``(chat_name, chat_topic)`` of the home channel, or ``(None,
        None)``. The receiving bot may not see the user's private home
        channel, so the orchestrator profile's bot — which is IN it, by
        provisioning — is the second candidate. Fail-closed: unresolvable
        labels read as ``None`` (the session prompt falls back to the
        id-derived label), never a stale bound-channel label."""
        clients = []
        own = getattr(self, "_client", None)
        if own is not None:
            clients.append(own)
        runner = getattr(self, "gateway_runner", None)
        lookup = getattr(runner, "_adapters_for_profile", None)
        if callable(lookup):
            try:
                peer = lookup(orchestrator).get(getattr(self, "platform", None))
                client = getattr(peer, "_client", None)
                if client is not None and client is not own:
                    clients.append(client)
            except Exception:
                logger.debug(
                    "[%s] Concierge: the orchestrator adapter's client was "
                    "unreachable for home-channel labels",
                    getattr(self, "name", "discord"), exc_info=True)
        labels = getattr(self, "_guild_channel_labels", None)
        for client in clients:
            try:
                channel = client.get_channel(int(home))
                pair = (labels(channel) if channel is not None and callable(labels)
                        else None)
            except Exception:
                logger.debug(
                    "[%s] Concierge: reading the home channel %s's labels "
                    "failed; the session prompt falls back to the id label",
                    getattr(self, "name", "discord"), home, exc_info=True)
                continue
            if isinstance(pair, tuple) and len(pair) == 2:
                return pair[0], pair[1]
        return None, None

    # ── The home-lane playback map (design: voice-home-session-rekey) ──────

    # home channel id (str) -> the guild whose voice call captured the
    # utterance; populated ONLY by the orchestrator lane's re-key, consumed
    # by the auto-TTS gate below, Discord play_tts and the runner's
    # verbal-ack guild lookup. ``None`` until the first re-key (a plain
    # concierge-less install never populates it).
    _voice_home_lanes: Any = None

    def _concierge_voice_home_guild_for(self, chat_id):
        """The guild whose voice call serves *chat_id*'s re-keyed home
        session, or ``None`` — a pure map lookup; the callers apply their
        own liveness (``is_in_voice_channel`` / ``voice_mixer_active``),
        mirroring how the bound-channel paths do."""
        lanes = self._voice_home_lanes
        if not isinstance(lanes, dict):
            return None
        guild = lanes.get(str(chat_id) if chat_id is not None else "")
        return guild if isinstance(guild, int) else None

    def _concierge_voice_home_lane_register(self, home, guild) -> None:
        """Record the home→guild playback lane, pruning entries whose voice
        call went away (the map stays bounded by LIVE voice sessions). Never
        raises: a failed registration degrades playback to the
        bound-channel seams."""
        try:
            lanes = self._voice_home_lanes
            if not isinstance(lanes, dict):
                lanes = self._voice_home_lanes = {}
            for key, gid in list(lanes.items()):
                try:
                    live = self.is_in_voice_channel(gid)
                except Exception:
                    logger.debug(
                        "[%s] Concierge: probing voice liveness for guild %s "
                        "failed; pruning the stale home lane",
                        getattr(self, "name", "discord"), gid, exc_info=True)
                    live = False
                if not live:
                    del lanes[key]
            lanes[str(home)] = int(guild)
        except Exception:
            logger.debug(
                "[%s] Concierge: the voice home-lane registration failed",
                getattr(self, "name", "discord"), exc_info=True)

    def _should_auto_tts_for_chat(self, chat_id) -> bool:
        """Base gate, plus the home-lane mirror: a spoken turn re-keyed onto
        the home session auto-TTSes exactly when the VOICE-BINDING channel
        would (the mirror resolves the lane's guild → its bound channel →
        the base verdict), so ``/voice on|tts|off`` semantics keep owning
        spoken turns. Only VOICE-type events reach the auto-TTS path, so
        the mirror never speaks a typed reply. Fail-closed to the base
        verdict on any lookup fault."""
        if super()._should_auto_tts_for_chat(chat_id):
            return True
        try:
            guild = self._concierge_voice_home_guild_for(chat_id)
            if guild is None or not self.is_in_voice_channel(guild):
                return False
            bound = self._voice_text_channels.get(guild)
        except Exception:
            logger.debug(
                "[%s] Concierge: the home-lane auto-TTS mirror failed; the "
                "base gate's verdict stands",
                getattr(self, "name", "discord"), exc_info=True)
            return False
        return (bound is not None
                and super()._should_auto_tts_for_chat(str(bound)))


    def _concierge_registry_or_none(self, source):
        """ONE registry read for the whole routing decision (rooms + bindings
        share it), or ``None`` when the registry is unreadable — degrade to
        the hub, message intake keeps working, ONE warning per channel (the
        M5b cardinality contract, unchanged)."""
        try:
            from concierge import registry

            return registry.load_registry()
        except Exception:
            self._concierge_warn_once(
                ("registry", str(getattr(source, "chat_id", "?")), ""),
                "[%s] concierge registry unreadable; per-channel routing "
                "skipped for channel %s",
                getattr(self, "name", "discord"), getattr(source, "chat_id", "?"),
                exc_info=True)
            return None

    def _concierge_profile_from_registry(self, source, reg) -> str | None:
        """Single-bot verdict's profile for *source* from a LOADED registry
        (the M5b ``resolve_target_profile`` call, fed the shared read)."""
        from concierge.routing import resolve_target_profile

        verdict = resolve_target_profile(
            reg.get("bindings") or {},
            channel_id=source.chat_id,
            parent_channel_id=source.parent_chat_id,
            author_is_bot=source.is_bot,
        )
        return verdict.profile if verdict.routed else None

    def _concierge_room_targets_from_registry(self, source, reg) -> list[str]:
        """Task 7.3 room verdict: the ordered member profiles this source's
        event must fan out to; ``[]`` when the channel is not a room, the
        message is bot-authored (anti-loop, pure layer), or every member was
        filtered out. Threads/forum posts inherit the parent channel's room
        (binding parity). Ghost members are skipped with the binding path's
        warn-once/fail-closed posture — a retired member never runs, and the
        live members still answer."""
        from concierge.routing import resolve_room_targets

        rooms = reg.get("rooms")
        if not isinstance(rooms, dict):
            return []
        room = rooms.get(str(getattr(source, "chat_id", "") or ""))
        if not isinstance(room, dict):
            parent = getattr(source, "parent_chat_id", None)
            room = rooms.get(str(parent)) if parent is not None else None
        if not isinstance(room, dict):
            return []
        targets = resolve_room_targets(
            room.get("members"),
            author_is_bot=bool(getattr(source, "is_bot", False)),
            # v1 mention semantics: see the module docstring — the shared
            # Discord identity cannot name a member, so mentioned_profiles
            # stays empty (no mention and @shared-bot are both broadcasts).
        )
        channel_id = str(getattr(source, "chat_id", "?"))
        live = []
        for member in targets:
            exists, stat_error = _profile_exists(member)
            if stat_error is not None:
                self._concierge_warn_once(
                    ("room-stat", channel_id, member),
                    "[%s] Concierge room member %r for channel %s could not "
                    "be checked (%s); skipping it for this fan-out — "
                    "investigate the profile directory/permissions before "
                    "re-provisioning",
                    getattr(self, "name", "discord"), member, channel_id,
                    stat_error)
                continue
            if not exists:
                self._concierge_warn_once(
                    ("room-ghost", channel_id, member),
                    "[%s] Concierge room member %r for channel %s does not "
                    "exist (bot retired?); skipping it — remove it with "
                    "/room remove",
                    getattr(self, "name", "discord"), member, channel_id)
                continue
            live.append(member)
        return live

    # ── Task 3: the spoken-summary relay (concierge.voice_relay) ──────────
    #
    # The seam: the base post-handler auto-TTS block runs, back to back on
    # the same task, ``self._wants_auto_tts(...)`` (base.py:4124) and then
    # ``await self._synthesize_auto_tts(text_content)`` (base.py:4507) —
    # both plain ``self`` methods, so the adapter MRO resolves them onto
    # THIS mixin. The relay substitutes the synthesized TEXT there for
    # member-lane voice events (marked by the fan-out driver's
    # ``_VOICE_RELAY_ATTR`` stamp): no second speaker exists, and the Task
    # 2 single-playback/dedup contracts hold by construction.

    def _wants_auto_tts(self, event, session_key, interrupt_event,
                        text_content, media_files) -> bool:
        """Base gate, plus the relay handoff: remember the member-lane
        voice event whose synthesis follows. The base chain calls
        ``_synthesize_auto_tts(text_content)`` immediately after this
        returns True — with NO await in between — so the event stashed
        here is consumed by the very next call on this task: a concurrent
        turn on the SHARED adapter (the hub lane) can never observe
        another turn's stash. Always reset — ``None`` for the hub's own
        spoken turns, non-voice input, and every no-TTS path — so a
        stash can never outlive its turn."""
        wants = super()._wants_auto_tts(
            event, session_key, interrupt_event, text_content, media_files)
        self._concierge_relay_tts_event = (
            event if wants and getattr(event, _VOICE_RELAY_ATTR, None) else None)
        return wants

    async def _synthesize_auto_tts(self, text_content):
        """Base synthesis, with the relay's text substitution for
        member-lane voice events: when the pending relay event summarizes
        successfully, the SUMMARY is what gets synthesized — the full
        answer still goes to the room as text, and playback stays the ONE
        pinned path. Every relay no-op (mode off/verbatim, a short
        answer, an aux failure) falls straight through to the base
        verbatim synthesis: audio must always arrive."""
        relay_event = getattr(self, "_concierge_relay_tts_event", None)
        self._concierge_relay_tts_event = None
        if relay_event is not None:
            summary = await self._concierge_voice_relay_summary(
                relay_event, text_content)
            if summary:
                return await super()._synthesize_auto_tts(summary)
        elif _voice_relay_settings(
                getattr(self, "gateway_runner", None)).mode == "summary":
            # Stash-drift visibility: a summary-mode synthesis with no
            # relay event in hand is a non-member turn — or drift in the
            # base gate→synthesis seam — and silently synthesizing
            # verbatim either way deserves at least a debug trace.
            logger.debug(
                "[%s] concierge voice relay: summary-mode synthesis with no "
                "relay event in hand (non-member turn or seam drift); "
                "synthesizing verbatim",
                getattr(self, "name", "discord"))
        return await super()._synthesize_auto_tts(text_content)

    async def _concierge_voice_relay_summary(
            self, event, answer: str) -> str | None:
        """The spoken summary of *answer* (a member lane's final answer)
        for a member-lane voice event, or ``None`` for verbatim synthesis.

        ``None`` with NO aux call: relay off/verbatim, or the answer
        at/below ``max_chars`` (already driving-appropriate). Otherwise
        the aux call rides the auxiliary machinery — task
        ``voice_relay``, per-task pinnable via ``auxiliary.voice_relay``
        in the launch-home config (unset = the machinery's auto route) —
        never the member profile's main model call path. It resolves in
        the scope the delivery seam already runs under: the launch scope,
        because the runner's profile binding unwound when the turn's
        handler returned (so the launch home's config/auth answer, the
        same placement rule as every ``concierge:`` key). FAIL-CLOSED:
        any failure (error, empty summary, reasoning-only response,
        timeout) warns and returns ``None`` — verbatim audio still
        arrives, and a relay bug can never break text delivery or the
        fan-out. The warning is per member TURN, not warn-once: an aux
        outage is transient, so a new failure must never stay silent."""
        settings = _voice_relay_settings(getattr(self, "gateway_runner", None))
        if settings.mode != "summary":
            return None
        if len(answer) <= settings.max_chars:
            return None
        member = str(getattr(event, _VOICE_RELAY_ATTR, "") or "?")
        try:
            from agent.auxiliary_client import (
                async_call_llm, extract_content_or_reasoning)

            response = await async_call_llm(
                task=_VOICE_RELAY_TASK,
                messages=[
                    {"role": "system", "content": _VOICE_RELAY_SYSTEM_PROMPT},
                    {"role": "user", "content": answer},
                ],
                temperature=0.3,
                max_tokens=_VOICE_RELAY_MAX_TOKENS)
            # CoT is never appropriate for SPEECH: reject the reasoning
            # fallback outright — a response whose budget landed in
            # reasoning (content empty) is a FAILED summary here, not one,
            # so the fail-closed branch speaks the full answer instead.
            summary = extract_content_or_reasoning(
                response, max_reasoning_chars=0).strip()
            # The empty check must see the text that would actually be
            # SPOKEN: a disobedient summarizer's markdown is stripped
            # before it, so a markdown-only summary fails closed too
            # instead of dying at the base synthesis with no audio.
            from tools.tts_text_normalize import _strip_markdown_for_tts

            summary = _strip_markdown_for_tts(summary).strip()
            if not summary:
                raise ValueError("empty summary")
            return summary
        except Exception as e:
            # A plain per-turn warning, NOT a warn-once key: an aux outage
            # is transient, not a durable registry state, so a new failure
            # class for the same member must never be silent forever. Once
            # per turn by construction — one aux call per member turn.
            logger.warning(
                "[%s] concierge voice relay: summarizing member %s's answer "
                "failed (%s); speaking the full answer instead",
                getattr(self, "name", "discord"), member, e, exc_info=True)
            return None
