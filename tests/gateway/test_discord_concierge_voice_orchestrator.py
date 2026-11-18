"""Concierge voice ingress: the speaker→orchestrator lane (design:
concierge/docs/voice-home-session-rekey.md), admission-reconciled —
binding/room win for voice — and re-keyed onto the SPEAKER'S HOME-CHANNEL
SESSION.

``_restate_voice_routing_stamps`` runs the NORMAL registry verdict FIRST —
a binding or room verdict claims the channel exactly as for typed input (a
user speaking in a worker's channel is talking to that worker; design:
binding routing untouched) — and the orchestrator LANE consumes the
verdict tail's EXISTING ``_CONCIERGE_ORCHESTRATOR_ATTR`` stamp: stamped
exactly when no binding/room verdict matched AND the author has a LIVE
orchestrator. The lane then re-keys the source onto the user's RECORDED
home channel (operator decision 2026-11-17): the spoken turn runs as the
speaker's orchestrator ON THE SAME SESSION typed turns in the home channel
run — ``agent:<orch>:discord:group:<home>:<speaker>`` — not a fresh session
keyed to the voice-bound text channel. Per-speaker by construction (the key
carries the user id), so a shared voice channel works multi-user. Admission
reconciles by construction: the re-keyed source re-runs the registry
verdict, so it carries the IDENTICAL stamps a fresh ``build_source`` for the
home channel gets, and ``_concierge_dynamic_runtime_profile`` confirms via
the home channel's BINDING (branch (a)). The orchestrator's own final turn
already contains the dispatched-and-summarized worker results, so the
existing voice relay speaks the right answer (no relay changes).

Why the verdict must run FIRST (the admission wedge this suite pins): the
pre-reconciliation order re-homed ANY channel's speaker to their
orchestrator BEFORE the verdict — but admission confirms an orchestrator
stamp ONLY on unbound, non-room channels (the binding (a) and room (b)
blocks return early), so a speaker with a live orchestrator in a BOUND or
ROOM channel was re-homed to a profile admission refuses →
``profile_mismatch`` → the utterance DROPPED — the one outcome the routing
invariants forbid.

Asserted at the RESTAMP level, mirroring the voice-ingress suite's rig
(the REAL DiscordAdapter through the live voice ingress; the wrap's
delegations observed at the INSTALLED runner handler):

- LIVE orchestrator + UNBOUND channel → the personal lane: one delegation
  on the orchestrator's profile, no fan-out (existing behavior preserved);
  with a recorded home → the HOME session key (this change's contract).
- Two speakers in one shared voice channel → two home sessions (the key's
  user slot isolates them).
- The re-keyed source's stamps are IDENTICAL to a fresh ``build_source``
  for the home channel (profile via the binding branch, no orchestrator
  stamp, thread shape flattened to the home group channel).
- LIVE orchestrator + channel BOUND to another bot → the BINDING profile
  wins, NOT the orchestrator (admission branch (a) confirms the binding;
  this row previously asserted the hijack — the flip IS the fix). A ROOM
  channel keeps its fan-out. Neither re-keys even with a recorded home.
- NO user entry / no orchestrator → byte-for-byte today's verdicts: room
  fan-out, bound 1:1, unbound hub (the copied ingress cases).
- GHOST / STAT-ERROR orchestrator + unbound → warn-once under the verdict
  tail's existing keys (the liveness posture lives wholly in the stamp —
  the lane does no second pass) + fall-through to the hub lane.
- FAIL-CLOSED re-key ladder: no recorded home, home unbound, home bound to
  another profile, home a room, recorded guild ≠ voice guild, users-read
  fault → today's bound-channel session stands.
- Playback still targets the VOICE channel: the auto-TTS gate mirrors the
  bound channel's /voice mode, play_tts resolves the lane guild, and the
  verbal-ack guild lookup finds the voice call — none keyed on the
  re-keyed chat id. The transcript echo still lands in the BOUND channel,
  and the per-channel prompt/skills follow the post-hook source.
- BOT-authored source → the lane never applies (a bot never resolves to
  an orchestrator lane).
- The existing gates (enabled, multiplex, route-rejected, explicit config
  route) stay AHEAD of the verdict and the lane.
- NEVER raises: a failure inside the lane leaves the verdict's routing in
  place (fail-closed, intake never breaks).
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from concierge import registry
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageType
from gateway.session import SessionSource

from tests.gateway.conftest import _ensure_discord_mock

_ensure_discord_mock()

from plugins.platforms.discord import concierge_mixin
from plugins.platforms.discord.adapter import DiscordAdapter
from plugins.platforms.discord.concierge_mixin import ConciergeDiscordMixin

_GUILD, _SPEAKER, _CHANNEL = 1, 42, 700
_MEMBER_A = "bot-reminders-a1b2"
_MEMBER_B = "bot-researcher-b2c3"
_ORCH = "bot-orchestrator-7f3a"
_ORCH_B = "bot-orchestrator-b9c4"
_GHOST_ORCH = "bot-orchestrator-ghost0"
_STAT_ORCH = "bot-orchestrator-stat0"
_SPEAKER_B = 43
_HOME_A = "9001"               # the speaker's recorded home channel
_HOME_B = "9002"               # the second speaker's home channel


@pytest.fixture
def voice_rig(tmp_path, monkeypatch):
    """Multiplexed hub gateway with concierge on, plus the voice attributes
    ``_handle_voice_channel_input`` reads (the ingress rig verbatim, plus the
    speaker's orchestrator profile so a users entry can name a LIVE profile;
    the adapter is the REAL DiscordAdapter, so ``build_source``, the wrap
    and the fan-out machinery are all live)."""
    ConciergeDiscordMixin._concierge_warned.clear()  # per-test warn-once hygiene
    root = tmp_path
    for name in (_MEMBER_A, _MEMBER_B, _ORCH, _ORCH_B):
        (root / "profiles" / name).mkdir(parents=True)
        (root / "profiles" / name / ".env").write_text("OPENAI_API_KEY=sk-x\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    (root / "concierge").mkdir(exist_ok=True)

    # gateway/run.py's import chain pulls hermes_bootstrap, whose module-level
    # PM activation stats the REAL ~/.hermes on a live-install checkout — stub
    # it before the import (same shape gateway/run.py tolerates when absent).
    _bootstrap = types.ModuleType("hermes_bootstrap")
    _bootstrap._happy_eyeballs_create_connection = socket.create_connection
    sys.modules.setdefault("hermes_bootstrap", _bootstrap)

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.platforms = {Platform.DISCORD: PlatformConfig(enabled=True, extra={})}
    runner.config.concierge = {"enabled": True}
    runner.pairing_store = SimpleNamespace()
    runner.pairing_stores = {}
    runner._primary_profile_name = "default"
    runner._session_db = None
    runner.session_store = MagicMock()
    runner._is_user_authorized = MagicMock(return_value=True)
    runner._voice_mode = {}

    adapter = object.__new__(DiscordAdapter)
    BasePlatformAdapter.__init__(
        adapter, PlatformConfig(enabled=True, extra={}), Platform.DISCORD)
    adapter.gateway_runner = runner
    # Voice binding attrs the runner reads (attribute names mirror production).
    adapter._client = None  # no discord client: guild/channel lookups degrade
    adapter._voice_text_channels = {_GUILD: _CHANNEL}
    adapter._voice_sources = {}
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {}
    return SimpleNamespace(root=root, runner=runner, adapter=adapter)


async def _install_seam_recorder(monkeypatch, adapter):
    """Record every event the concierge wrap DELEGATES (the installed runner
    handler), and replace ``_process_message_background`` with the minimal
    stand-in whose only job is the seam this suite guards: awaiting the
    adapter's INSTALLED message handler — under concierge, the real
    ``_concierge_inbound_wrap``. The wrap's fan-out machinery and every
    member's dispatch back through the real base ``handle_message`` run
    live (the ingress suite's recorder, verbatim)."""
    records = []

    async def _runner_handler(event):
        records.append((
            getattr(event.source, "profile", None),
            event.message_type,
            getattr(event.raw_message, "guild_id", None),
        ))

    adapter.set_message_handler(_runner_handler)

    async def _fake_background(event, session_key):
        await adapter._message_handler(event)

    monkeypatch.setattr(adapter, "_process_message_background", _fake_background)
    return records


async def _speak(rig, *, join_source: bool) -> None:
    """One transcribed utterance through the REAL voice ingress (the ingress
    suite's helper, verbatim): ``join_source`` stores the ``/voice join``
    message's source SERIALIZED first — the copy path — else the
    programmatic ``build_source`` path runs."""
    if join_source:
        join = rig.adapter.build_source(
            chat_id=str(_CHANNEL), chat_type="group", user_id=str(_SPEAKER),
            user_name="Operator", guild_id=str(_GUILD))
        rig.adapter._voice_sources[_GUILD] = join.to_dict()
    await rig.runner._handle_voice_channel_input(
        _GUILD, _SPEAKER, "brief me on the day", adapter=rig.adapter)


async def _drain(adapter, *, timeout: float = 5.0) -> None:
    """Run every task the ingress spawned to completion — bounded, so a
    wedged path fails the test instead of hanging it."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        pending = [t for t in list(adapter._background_tasks)
                   if not t.done() and t is not asyncio.current_task()]
        if not pending:
            return
        await asyncio.gather(*pending)
    raise AssertionError("voice ingress tasks did not settle within "
                         f"{timeout}s")


def _copy_source(rig) -> SessionSource:
    """A stamp-less source, the shape the ``/voice join`` copy path feeds the
    restamp: a serialized copy of a built source (``to_dict`` drops the
    process-local concierge stamps by design), so every stamp present after
    ``_restate_voice_routing_stamps`` was placed by the hook itself."""
    join = rig.adapter.build_source(
        chat_id=str(_CHANNEL), chat_type="group", user_id=str(_SPEAKER),
        user_name="Operator", guild_id=str(_GUILD))
    return SessionSource.from_dict(join.to_dict())


# ── Binding/room win for voice (the admission reconciliation) ───────────────


@pytest.mark.asyncio
async def test_live_orchestrator_does_not_hijack_a_room_channel(
        voice_rig, monkeypatch):
    """THE reconciliation contract (admission branch (b) owns room
    channels): a room channel keeps its fan-out even when the speaker has
    a live orchestrator — the same channel that fans out for an
    orchestrator-less speaker fans out here, copy path. A re-home to the
    orchestrator would be a profile admission refuses (the ``profile_mismatch``
    drop), and a room is a conversation of its own."""
    registry.create_room(str(_CHANNEL), [_MEMBER_A, _MEMBER_B])
    registry.set_user(str(_SPEAKER), orchestrator=_ORCH)
    records = await _install_seam_recorder(monkeypatch, voice_rig.adapter)

    await _speak(voice_rig, join_source=True)
    await _drain(voice_rig.adapter)

    assert records == [
        (_MEMBER_A, MessageType.VOICE, _GUILD),
        (_MEMBER_B, MessageType.VOICE, _GUILD),
    ]


@pytest.mark.asyncio
async def test_live_orchestrator_keeps_room_fanout_on_the_programmatic_path(
        voice_rig, monkeypatch):
    """The programmatic join builds the source via ``build_source``, which
    stamps the ROOM targets — and the reconciled order re-runs the verdict
    (re-stamping them), so the fan-out survives this path too: no
    orchestrator hijack, no stale-stamp clearing against a live room."""
    registry.create_room(str(_CHANNEL), [_MEMBER_A, _MEMBER_B])
    registry.set_user(str(_SPEAKER), orchestrator=_ORCH)
    records = await _install_seam_recorder(monkeypatch, voice_rig.adapter)

    await _speak(voice_rig, join_source=False)
    await _drain(voice_rig.adapter)

    assert records == [
        (_MEMBER_A, MessageType.VOICE, _GUILD),
        (_MEMBER_B, MessageType.VOICE, _GUILD),
    ]


@pytest.mark.asyncio
async def test_live_orchestrator_does_not_hijack_a_bound_channel(
        voice_rig, monkeypatch):
    """The BINDING profile wins over the speaker's orchestrator (design:
    binding routing untouched — a user speaking in a worker's channel is
    talking to that worker). Admission rationale: branch (a) confirms a
    binding stamp only, so an orchestrator re-home on a bound channel is a
    profile admission refuses — ``profile_mismatch``, the utterance DROPPED,
    the one outcome the routing invariants forbid. This row previously
    asserted the hijack; the flip IS the fix."""
    registry.bind_channel(
        str(_CHANNEL), profile=_MEMBER_A, blueprint="reminders",
        guild_id=str(_GUILD))
    registry.set_user(str(_SPEAKER), orchestrator=_ORCH)
    records = await _install_seam_recorder(monkeypatch, voice_rig.adapter)

    await _speak(voice_rig, join_source=True)
    await _drain(voice_rig.adapter)

    assert records == [(_MEMBER_A, MessageType.VOICE, _GUILD)]


def test_room_stamp_survives_the_restamp_no_orchestrator_hijack(voice_rig):
    """Restamp-level: on a room channel the verdict's room-targets stamp
    SURVIVES the restamp and the profile is left unstamped for the
    fan-out — the orchestrator lane never runs (a room verdict leaves no
    orchestrator stamp to consume). The pre-reconciliation order cleared
    the stamp and re-homed here: the admission wedge."""
    registry.create_room(str(_CHANNEL), [_MEMBER_A, _MEMBER_B])
    registry.set_user(str(_SPEAKER), orchestrator=_ORCH)
    source = voice_rig.adapter.build_source(
        chat_id=str(_CHANNEL), chat_type="group", user_id=str(_SPEAKER),
        user_name="Operator", guild_id=str(_GUILD))
    assert getattr(source, concierge_mixin._ROOM_TARGETS_ATTR, None)  # stamped

    voice_rig.adapter._restate_voice_routing_stamps(source)

    assert source.profile is None  # no orchestrator re-home on a room
    assert getattr(source, concierge_mixin._ROOM_TARGETS_ATTR, None)  # intact
    assert getattr(source, concierge_mixin._CONCIERGE_ORCHESTRATOR_ATTR,
                   None) is None


# ── The personal lane: unbound + live orchestrator ──────────────────────────


@pytest.mark.asyncio
async def test_live_orchestrator_takes_the_personal_lane_on_unbound(
        voice_rig, monkeypatch):
    """The lane's own claim, now admission-shaped by construction: an
    UNBOUND channel (the only shape the verdict tail stamps) with a
    live-orchestrator speaker delegates 1:1 on the orchestrator's profile —
    no fan-out, no hub; the existing behavior, preserved."""
    registry.set_user(str(_SPEAKER), orchestrator=_ORCH)
    records = await _install_seam_recorder(monkeypatch, voice_rig.adapter)

    await _speak(voice_rig, join_source=True)
    await _drain(voice_rig.adapter)

    assert records == [(_ORCH, MessageType.VOICE, _GUILD)]


def test_unbound_lane_consumes_the_verdicts_orchestrator_stamp(voice_rig):
    """Restamp-level proof of the reconciled order: the verdict RUNS (its
    unbound tail stamps the orchestrator) and the LANE consumes the stamp
    into ``source.profile`` — the re-home admission's branch (c) confirms
    by construction (unbound, non-room, live orchestrator), with no
    second users-section read or liveness pass in the lane."""
    registry.set_user(str(_SPEAKER), orchestrator=_ORCH)
    source = _copy_source(voice_rig)

    voice_rig.adapter._restate_voice_routing_stamps(source)

    assert source.profile == _ORCH
    # The verdict's stamp stays on the source: the wrap's fallback re-home
    # is idempotent (same profile), and admission needs only the profile.
    assert getattr(source, concierge_mixin._CONCIERGE_ORCHESTRATOR_ATTR,
                   None) == _ORCH
    assert getattr(source, concierge_mixin._ROOM_TARGETS_ATTR, None) is None


# ── The home-session re-key (design: voice-home-session-rekey) ─────────────


def _seed_home(voice_rig, *, user=_SPEAKER, orchestrator=_ORCH,
               home=_HOME_A, bind=True):
    """The provisioning shape: the user's entry RECORDS their home channel
    and guild, and the home channel is BOUND to the orchestrator
    (``bind_channel`` — concierge/home_space.py)."""
    registry.set_user(str(user), orchestrator=orchestrator,
                      home_channel=home, guild_id=str(_GUILD))
    if bind:
        registry.bind_channel(str(home), profile=orchestrator,
                               blueprint="orchestrator", guild_id=str(_GUILD))


async def _install_home_session_recorder(monkeypatch, rig):
    """The seam recorder's twin for the home-session contract: one row per
    DELEGATED turn with the profile, the post-hook ``chat_id`` and the
    DERIVED session key (the same ``_session_key_for_source`` the runner's
    ``_handle_message`` uses), plus the source object for playback rows."""
    rows = []

    async def _runner_handler(event):
        rows.append(SimpleNamespace(
            profile=getattr(event.source, "profile", None),
            chat_id=str(event.source.chat_id),
            session_key=rig.runner._session_key_for_source(event.source),
            source=event.source))

    rig.adapter.set_message_handler(_runner_handler)

    async def _fake_background(event, session_key):
        await rig.adapter._message_handler(event)

    monkeypatch.setattr(rig.adapter, "_process_message_background", _fake_background)
    return rows


async def _speak_as(rig, user, text="brief me on the day", *, join_source=True):
    """One transcribed utterance through the REAL voice ingress, as *user*
    (the cached join source is per-guild; the ingress re-stamps THIS
    speaker's identity on it, run_voice.py's ``_voice_input_source``)."""
    if join_source:
        join = rig.adapter.build_source(
            chat_id=str(_CHANNEL), chat_type="group", user_id=str(user),
            user_name=f"User{user}", guild_id=str(_GUILD))
        rig.adapter._voice_sources[_GUILD] = join.to_dict()
    await rig.runner._handle_voice_channel_input(
        _GUILD, user, text, adapter=rig.adapter)


@pytest.mark.asyncio
async def test_spoken_turn_lands_on_the_speakers_home_session(
        voice_rig, monkeypatch):
    """THE contract: an unbound voice-bound channel + a speaker whose entry
    records a home channel bound to their orchestrator → the spoken turn
    delegates ON THE HOME SESSION — ``agent:<orch>:discord:group:<home>:
    <speaker>`` — the SAME key a typed turn in the home channel derives,
    not the fresh ``…:<bound-channel>`` session of the old lane."""
    _seed_home(voice_rig)
    rows = await _install_home_session_recorder(monkeypatch, voice_rig)

    await _speak_as(voice_rig, _SPEAKER)
    await _drain(voice_rig.adapter)

    assert [(r.profile, r.chat_id) for r in rows] == [(_ORCH, _HOME_A)]
    assert rows[0].session_key == f"agent:{_ORCH}:discord:group:{_HOME_A}:{_SPEAKER}"


@pytest.mark.asyncio
async def test_two_speakers_get_two_home_sessions(voice_rig, monkeypatch):
    """Per-speaker isolation: two speakers in ONE shared voice channel,
    each with their own orchestrator and home channel → two delegations on
    two DIFFERENT home sessions (the key's user slot isolates them by
    construction — the shared voice channel never shares a session)."""
    _seed_home(voice_rig, user=_SPEAKER, orchestrator=_ORCH, home=_HOME_A)
    _seed_home(voice_rig, user=_SPEAKER_B, orchestrator=_ORCH_B, home=_HOME_B)
    rows = await _install_home_session_recorder(monkeypatch, voice_rig)

    await _speak_as(voice_rig, _SPEAKER, "plan my day")
    await _drain(voice_rig.adapter)
    await _speak_as(voice_rig, _SPEAKER_B, "water the plants")
    await _drain(voice_rig.adapter)

    assert [(r.profile, r.chat_id, r.session_key) for r in rows] == [
        (_ORCH, _HOME_A, f"agent:{_ORCH}:discord:group:{_HOME_A}:{_SPEAKER}"),
        (_ORCH_B, _HOME_B, f"agent:{_ORCH_B}:discord:group:{_HOME_B}:{_SPEAKER_B}"),
    ]


def test_rekeyed_source_carries_fresh_home_stamps(voice_rig):
    """Identical-stamps proof: after the restamp re-keyed it, the source is
    byte-shaped like a FRESH ``build_source`` for the home channel — the
    binding branch's profile, NO orchestrator stamp (a fresh home source
    never carries one) — and a thread-bound join is flattened to the home
    channel's group shape (``build_session_key`` reads exactly these)."""
    _seed_home(voice_rig)
    source = _copy_source(voice_rig)
    source.chat_type, source.thread_id = "thread", "77"
    source.parent_chat_id = str(_CHANNEL)

    voice_rig.adapter._restate_voice_routing_stamps(source)

    fresh = voice_rig.adapter.build_source(
        chat_id=str(_HOME_A), chat_type="group", user_id=str(_SPEAKER),
        user_name="Operator", guild_id=str(_GUILD))
    assert source.profile == fresh.profile == _ORCH
    assert source.chat_id == fresh.chat_id == _HOME_A
    assert source.chat_type == "group"
    assert source.thread_id is None and source.parent_chat_id is None
    for attr in (concierge_mixin._CONCIERGE_ORCHESTRATOR_ATTR,
                 concierge_mixin._ROOM_TARGETS_ATTR):
        assert getattr(source, attr, None) is None
        assert getattr(fresh, attr, None) is None


@pytest.mark.asyncio
async def test_bound_channel_with_recorded_home_keeps_binding_routing(
        voice_rig, monkeypatch):
    """A recorded home NEVER re-keys a channel a binding owns: speaking in
    a worker's channel talks to that worker, on that channel's session."""
    registry.bind_channel(
        str(_CHANNEL), profile=_MEMBER_A, blueprint="reminders",
        guild_id=str(_GUILD))
    _seed_home(voice_rig)
    rows = await _install_home_session_recorder(monkeypatch, voice_rig)

    await _speak_as(voice_rig, _SPEAKER)
    await _drain(voice_rig.adapter)

    assert [(r.profile, r.chat_id) for r in rows] == [(_MEMBER_A, str(_CHANNEL))]


@pytest.mark.asyncio
async def test_room_channel_with_recorded_home_keeps_fanout(
        voice_rig, monkeypatch):
    """A recorded home NEVER re-keys a ROOM channel: the fan-out owns the
    channel, every member lane runs on the channel's own shape."""
    registry.create_room(str(_CHANNEL), [_MEMBER_A, _MEMBER_B])
    _seed_home(voice_rig)
    rows = await _install_home_session_recorder(monkeypatch, voice_rig)

    await _speak_as(voice_rig, _SPEAKER)
    await _drain(voice_rig.adapter)

    assert [(r.profile, r.chat_id) for r in rows] == [
        (_MEMBER_A, str(_CHANNEL)), (_MEMBER_B, str(_CHANNEL))]


@pytest.mark.asyncio
async def test_no_home_recorded_keeps_the_bound_channel_session(
        voice_rig, monkeypatch):
    """No recorded home → today's lane stands: the orchestrator runs the
    spoken turn, but on the BOUND channel's session (fail-closed row —
    the re-key is an upgrade, never a requirement)."""
    registry.set_user(str(_SPEAKER), orchestrator=_ORCH)
    rows = await _install_home_session_recorder(monkeypatch, voice_rig)

    await _speak_as(voice_rig, _SPEAKER)
    await _drain(voice_rig.adapter)

    assert [(r.profile, r.chat_id) for r in rows] == [(_ORCH, str(_CHANNEL))]
    assert rows[0].session_key == (
        f"agent:{_ORCH}:discord:group:{_CHANNEL}:{_SPEAKER}")


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [
    pytest.param(None, id="home-unbound"),
    pytest.param("other_profile", id="home-bound-elsewhere"),
    pytest.param("room", id="home-is-a-room"),
    pytest.param("guild_mismatch", id="recorded-guild-mismatch"),
])
async def test_rekey_shape_mismatch_keeps_todays_session(
        voice_rig, monkeypatch, seed):
    """The fail-closed ladder: a home channel that does not resolve to
    exactly the speaker's orchestrator (unbound / bound to another profile /
    a room / recorded in another guild) never re-keys — the spoken turn
    keeps today's bound-channel session, orchestrator re-home and all."""
    registry.set_user(str(_SPEAKER), orchestrator=_ORCH,
                      home_channel=_HOME_A,
                      guild_id="999" if seed == "guild_mismatch" else str(_GUILD))
    if seed is None:
        pass  # home recorded, no binding
    elif seed == "other_profile":
        registry.bind_channel(_HOME_A, profile=_MEMBER_A, blueprint="reminders",
                              guild_id=str(_GUILD))
    elif seed == "room":
        registry.create_room(_HOME_A, [_MEMBER_A])
    rows = await _install_home_session_recorder(monkeypatch, voice_rig)

    await _speak_as(voice_rig, _SPEAKER)
    await _drain(voice_rig.adapter)

    assert [(r.profile, r.chat_id) for r in rows] == [(_ORCH, str(_CHANNEL))]


def test_users_read_failure_keeps_todays_lane(voice_rig, monkeypatch):
    """A registry accessor fault inside the lane is fail-closed: the
    spoken turn keeps the stamp's plain re-home (today's routing), never
    raises."""
    from concierge import registry as registry_module

    registry.set_user(str(_SPEAKER), orchestrator=_ORCH)
    monkeypatch.setattr(
        registry_module, "get_user",
        lambda user_id: (_ for _ in ()).throw(RuntimeError("registry fault")))
    source = _copy_source(voice_rig)

    voice_rig.adapter._restate_voice_routing_stamps(source)  # must not raise

    assert source.profile == _ORCH
    assert source.chat_id == str(_CHANNEL)  # no re-key over a fault


@pytest.mark.asyncio
async def test_playback_still_targets_the_voice_channel(
        voice_rig, monkeypatch):
    """The re-key must not leak into playback: the auto-TTS gate mirrors
    the BOUND channel's ``/voice`` mode for the home id, ``play_tts``
    resolves the lane's VOICE guild (never a file attachment in the home
    channel), and the verbal-ack guild lookup finds the voice call — all
    while ``/voice off`` on the bound channel silences the mirror."""
    _seed_home(voice_rig)
    rows = await _install_home_session_recorder(monkeypatch, voice_rig)
    played, sent = [], []

    async def _play(guild_id, path, **kw):
        played.append((guild_id, path))
        return True

    async def _send(**kw):
        sent.append(kw)
        return SimpleNamespace(success=True)

    monkeypatch.setattr(voice_rig.adapter, "is_in_voice_channel", lambda gid: True)
    monkeypatch.setattr(voice_rig.adapter, "play_in_voice_channel", _play)
    monkeypatch.setattr(voice_rig.adapter, "send_voice", _send)
    monkeypatch.setattr(voice_rig.adapter, "voice_mixer_active", lambda gid: True)
    # Prime the /voice state DIRECTLY (the sets the base gate reads): the
    # runner's _apply_voice_mode persists to _VOICE_MODE_PATH, which is
    # import-time-bound to the REAL home — a test must never write it.
    voice_rig.adapter._auto_tts_enabled_chats.add(str(_CHANNEL))

    await _speak_as(voice_rig, _SPEAKER)
    await _drain(voice_rig.adapter)

    assert rows and rows[0].chat_id == _HOME_A  # the turn IS home-keyed…
    assert voice_rig.adapter._should_auto_tts_for_chat(_HOME_A)  # …the gate mirrors the binding…
    await voice_rig.adapter.play_tts(chat_id=_HOME_A, audio_path="x.ogg")
    assert played == [(_GUILD, "x.ogg")] and not sent  # …and playback stays in the VC
    assert voice_rig.runner._voice_ack_guild_for(rows[0].source) == _GUILD

    # /voice off on the BOUND channel silences the home-id mirror.
    voice_rig.adapter._auto_tts_enabled_chats.discard(str(_CHANNEL))
    voice_rig.adapter._auto_tts_disabled_chats.add(str(_CHANNEL))
    assert not voice_rig.adapter._should_auto_tts_for_chat(_HOME_A)


@pytest.mark.asyncio
async def test_transcript_echo_still_targets_the_bound_channel(
        voice_rig, monkeypatch):
    """The transcript echo rides the BOUND text channel (the re-key never
    moves it), and the per-channel prompt/skills follow the POST-HOOK
    source: the home channel's context on a re-keyed turn, the bound
    channel's otherwise."""
    _seed_home(voice_rig)
    await _install_home_session_recorder(monkeypatch, voice_rig)
    echo_channel = SimpleNamespace(sent=[], id=_CHANNEL)
    home_channel = SimpleNamespace(sent=[], id=_HOME_A)  # must stay echo-silent

    async def _send(text, **kw):
        echo_channel.sent.append(text)

    async def _send_home(text, **kw):
        home_channel.sent.append(text)

    echo_channel.send = _send
    echo_channel.gets = []
    # A duck-typed client: guild/member lookups degrade (None) exactly like
    # the rig's None client, while get_channel serves the echo target (the
    # labels lookup may also ask for the HOME channel — it must stay silent).
    channels = {int(_CHANNEL): echo_channel, int(_HOME_A): home_channel}

    def _get_channel(cid):
        echo_channel.gets.append(cid)
        return channels.get(cid)

    voice_rig.adapter._client = SimpleNamespace(
        get_guild=lambda gid: None, get_user=lambda uid: None,
        get_channel=_get_channel)
    prompts, skills = [], []
    monkeypatch.setattr(voice_rig.adapter, "_resolve_channel_prompt",
                        lambda cid, parent: (prompts.append((cid, parent)) or None))
    monkeypatch.setattr(voice_rig.adapter, "_resolve_channel_skills",
                        lambda cid, parent: (skills.append((cid, parent)) or None))

    await _speak_as(voice_rig, _SPEAKER)
    await _drain(voice_rig.adapter)

    assert len(echo_channel.sent) == 1          # the transcript echo…
    assert not home_channel.sent                 # …lands in the BOUND channel only
    assert int(_CHANNEL) in echo_channel.gets
    assert prompts == [(_HOME_A, None)]         # the home session's context…
    assert skills == [(_HOME_A, None)]          # …on the re-keyed turn

    # Without a re-keyable home the context stays the bound channel's.
    prompts.clear(), skills.clear()
    echo_channel.sent.clear(), echo_channel.gets.clear()
    # update_user, not set_user: set_user is idempotent for the same
    # orchestrator and would KEEP the recorded home.
    registry.update_user(str(_SPEAKER), home_channel=None)
    await _speak_as(voice_rig, _SPEAKER, "and another thing")
    await _drain(voice_rig.adapter)
    assert prompts == [(str(_CHANNEL), None)]
    assert skills == [(str(_CHANNEL), None)]



# ── No user entry / no orchestrator: today's verdicts, byte-for-byte ────────


@pytest.mark.asyncio
async def test_speaker_without_user_entry_keeps_room_fanout(
        voice_rig, monkeypatch):
    """No users entry → the verdict's room fan-out runs exactly as today
    (the ingress suite's room contract, copied)."""
    registry.create_room(str(_CHANNEL), [_MEMBER_A, _MEMBER_B])
    records = await _install_seam_recorder(monkeypatch, voice_rig.adapter)

    await _speak(voice_rig, join_source=True)
    await _drain(voice_rig.adapter)

    assert records == [
        (_MEMBER_A, MessageType.VOICE, _GUILD),
        (_MEMBER_B, MessageType.VOICE, _GUILD),
    ]


@pytest.mark.asyncio
async def test_speaker_without_user_entry_keeps_bound_delegation(
        voice_rig, monkeypatch):
    """No users entry → the bound bot's 1:1 delegation runs exactly as today
    (the ingress suite's bound contract, copied)."""
    registry.bind_channel(
        str(_CHANNEL), profile=_MEMBER_A, blueprint="reminders",
        guild_id=str(_GUILD))
    records = await _install_seam_recorder(monkeypatch, voice_rig.adapter)

    await _speak(voice_rig, join_source=True)
    await _drain(voice_rig.adapter)

    assert records == [(_MEMBER_A, MessageType.VOICE, _GUILD)]


@pytest.mark.asyncio
async def test_speaker_without_user_entry_keeps_unbound_hub(
        voice_rig, monkeypatch):
    """No users entry → the unbound channel is plain delegation, identical
    to today's typed behavior (the ingress suite's unbound contract,
    copied)."""
    records = await _install_seam_recorder(monkeypatch, voice_rig.adapter)

    await _speak(voice_rig, join_source=True)
    await _drain(voice_rig.adapter)

    assert records == [(None, MessageType.VOICE, _GUILD)]


def test_user_entry_without_an_orchestrator_falls_through(voice_rig):
    """A users entry that names NO orchestrator (a hand-edited file) is not a
    lane claim: the verdict stamps the room-targets fan-out and the lane
    has no stamp to consume — the profile stays unstamped."""
    registry.create_room(str(_CHANNEL), [_MEMBER_A, _MEMBER_B])
    registry.set_user(str(_SPEAKER), orchestrator=_ORCH)
    path = voice_rig.root / "concierge" / "registry.json"
    data = json.loads(path.read_text())
    data["users"][str(_SPEAKER)] = {"home_channel": "9001"}  # no orchestrator
    path.write_text(json.dumps(data))
    source = _copy_source(voice_rig)

    voice_rig.adapter._restate_voice_routing_stamps(source)

    assert source.profile is None
    assert getattr(source, concierge_mixin._ROOM_TARGETS_ATTR, None)  # fan-out


# ── Dead orchestrators: warn-once + fall-through to the hub ─────────────────
#
# The liveness posture lives WHOLLY in the verdict tail's stamp (the lane does
# no second pass), so the warn keys are the stamp tail's own, unchanged —
# exactly the keys typed input on an unbound channel fires.


@pytest.mark.asyncio
async def test_ghost_orchestrator_warns_once_and_falls_through_to_hub(
        voice_rig, monkeypatch):
    """A GHOST orchestrator (the users entry names a profile that does not
    exist) never claims the lane: the verdict tail's fallback stamp warns
    ONCE under its existing ``orchestrator-ghost`` key and leaves the
    source unstamped — the unbound channel answers as the hub, the
    utterance is never dropped."""
    registry.set_user(str(_SPEAKER), orchestrator=_GHOST_ORCH)
    records = await _install_seam_recorder(monkeypatch, voice_rig.adapter)

    await _speak(voice_rig, join_source=True)
    await _drain(voice_rig.adapter)

    assert records == [(None, MessageType.VOICE, _GUILD)]
    assert (("orchestrator-ghost", str(_SPEAKER), _GHOST_ORCH)
            in ConciergeDiscordMixin._concierge_warned)


@pytest.mark.asyncio
async def test_stat_error_orchestrator_warns_once_and_falls_through_to_hub(
        voice_rig, monkeypatch):
    """A stat ERROR (the liveness check itself failed: EACCES, EIO, a
    stalled mount) is not "retired bot" — the verdict tail's fallback stamp
    warns ONCE under its existing ``orchestrator-stat`` key and leaves the
    source unstamped; the unbound channel answers as the hub. Routing
    fail-closed, but the operator action is "fix the filesystem", never
    "re-provision"."""
    from plugins.platforms.discord import concierge_voice

    registry.set_user(str(_SPEAKER), orchestrator=_STAT_ORCH)
    real_exists = concierge_voice._profile_exists

    def _stat_error_for_orchestrator(profile):
        if profile == _STAT_ORCH:
            return False, "PermissionError(13, 'Permission denied')"
        return real_exists(profile)

    monkeypatch.setattr(
        concierge_voice, "_profile_exists", _stat_error_for_orchestrator)
    records = await _install_seam_recorder(monkeypatch, voice_rig.adapter)

    await _speak(voice_rig, join_source=True)
    await _drain(voice_rig.adapter)

    assert records == [(None, MessageType.VOICE, _GUILD)]
    assert (("orchestrator-stat", str(_SPEAKER), _STAT_ORCH)
            in ConciergeDiscordMixin._concierge_warned)


# ── Anti-loop: a bot-authored source never resolves to a lane ───────────────


def test_bot_authored_source_skips_the_branch(voice_rig):
    """The room machinery's guard, mirrored: a bot-authored source never
    resolves to an orchestrator lane — the verdict's fallback tail equally
    guards bots (no stamp), and the lane skips them too, so the profile
    stays unstamped."""
    registry.set_user(str(_SPEAKER), orchestrator=_ORCH)
    source = _copy_source(voice_rig)
    source.is_bot = True

    voice_rig.adapter._restate_voice_routing_stamps(source)

    assert source.profile is None
    assert getattr(source, concierge_mixin._CONCIERGE_ORCHESTRATOR_ATTR,
                   None) is None


# ── The existing gates stay AHEAD of the verdict and the lane ──────────────


def test_disabled_concierge_gate_stays_ahead_of_the_branch(voice_rig):
    """Concierge off → the hook returns at the first gate: no orchestrator
    re-home, the source is untouched (zero behavior change)."""
    registry.set_user(str(_SPEAKER), orchestrator=_ORCH)
    voice_rig.runner.config.concierge = {"enabled": False}
    source = _copy_source(voice_rig)

    voice_rig.adapter._restate_voice_routing_stamps(source)

    assert source.profile is None


def test_non_multiplexed_gate_stays_ahead_of_the_branch(voice_rig):
    """A non-multiplexed gateway never runs concierge routing: the hook
    returns at the multiplex gate, no orchestrator re-home."""
    registry.set_user(str(_SPEAKER), orchestrator=_ORCH)
    voice_rig.runner.config.multiplex_profiles = False
    source = _copy_source(voice_rig)

    voice_rig.adapter._restate_voice_routing_stamps(source)

    assert source.profile is None


def test_route_rejected_gate_stays_ahead_of_the_branch(voice_rig):
    """A rejected explicit route stays rejected: the hook returns at the
    route gate, no orchestrator re-home."""
    registry.set_user(str(_SPEAKER), orchestrator=_ORCH)
    source = _copy_source(voice_rig)
    source.profile_route_rejected = True

    voice_rig.adapter._restate_voice_routing_stamps(source)

    assert source.profile is None


def test_explicit_config_route_stays_authoritative(voice_rig):
    """An explicit config-route profile stays authoritative over the
    registry (the hook's existing gate): the lane must not override it."""
    registry.set_user(str(_SPEAKER), orchestrator=_ORCH)
    source = _copy_source(voice_rig)
    source.profile = "bot-explicit-route"

    voice_rig.adapter._restate_voice_routing_stamps(source)

    assert source.profile == "bot-explicit-route"


# ── FAIL-CLOSED: a failure inside the lane never breaks intake ─────────────


def test_lane_rehome_failure_never_raises(voice_rig):
    """Never-raises, restructured for the reconciled order (the verdict now
    runs FIRST, so the lane's only fallible step is the re-home): a source
    whose ``profile`` assignment raises must not break the hook — the
    failure is swallowed, the verdict's routing stays in place (the stamp
    the wrap's fallback can still consume — else the hub answers), and
    intake never breaks."""
    registry.set_user(str(_SPEAKER), orchestrator=_ORCH)

    class _UnwritableProfile:
        """Delegates everything to the real copy source except ``profile``,
        whose ASSIGNMENT raises — the simulated in-lane failure."""

        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

        @property
        def profile(self):
            return None

        @profile.setter
        def profile(self, value):
            raise RuntimeError("simulated re-home failure")

    source = _UnwritableProfile(_copy_source(voice_rig))
    voice_rig.adapter._restate_voice_routing_stamps(source)  # must not raise

    # The verdict ran and stamped; the lane swallowed the re-home failure.
    assert getattr(source, concierge_mixin._CONCIERGE_ORCHESTRATOR_ATTR,
                   None) == _ORCH
