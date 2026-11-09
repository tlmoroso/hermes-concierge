"""Concierge voice lane delivery: a spoken room trigger's member-lane answers
are SPOKEN in the connected Discord voice channel — exactly once, at zero cost
when voice is not in use.

The delivery path this suite pins (verified in tree, the honest path that
actually fires for a member lane — NOT the runner's ``_send_voice_reply``):

- The room fan-out clones the VOICE-type trigger per member
  (``_concierge_run_member_turn``'s ``dataclasses.replace`` keeps
  ``message_type`` and ``raw_message``), so the member turn re-enters the REAL
  base chain (``handle_message`` → ``_process_message_background``) on the
  SHARED hub adapter — the adapter holding the ``/voice join`` binding.
- The base adapter's post-handler auto-TTS block is what speaks it:
  ``_process_message_background`` (base.py:4505) → ``_wants_auto_tts``
  (base.py:4124: per-chat /voice mode via ``_should_auto_tts_for_chat`` +
  VOICE message type) → ``_synthesize_auto_tts`` (base.py:4101) →
  ``_play_tts_file`` → the Discord ``play_tts`` override (adapter.py:3701,
  "in the guild's VC if joined, else as a file attachment") →
  ``play_in_voice_channel`` (adapter.py:3979).
- The runner-side auto voice reply (``run_turn_persistence`` →
  ``_should_send_voice_reply``/``_send_voice_reply``, run_voice.py:382/:430)
  is the DEDUPED path: a VOICE-type event it must decline
  (run_voice.py:425 — the base adapter already handled voice input) so the
  lane answer is played exactly ONCE.

Contracts:
- Spoken room trigger, voice mode on, hub adapter in the VC: every member's
  final answer is synthesized and played in the VC, in stored fan-out order;
  the room's text channel still receives the full text.
- No double-speak: exactly one synthesis + one playback per lane turn, and
  the runner-side guard declines every spoken lane event (armed in the
  scenario via the real ``_should_send_voice_reply`` — if it fired, its own
  synthesis + playback would double the counts).
- The hub's own spoken turn (an unbound channel) keeps today's auto voice
  reply behavior: same adapter-side path, one playback.
- Fail-closed / zero cost: with the voice session off (``/voice off``,
  leave, timeout — the state every no-VC transition leaves behind) a room
  fan-out synthesizes NOTHING. With the VC gone mid-turn (the drop race,
  mode still on) the designed fallback (adapter.py:3701-3708) applies: no VC
  playback, the audio reaches the user as a voice attachment, still exactly
  one synthesis per lane turn.

The scenario is end-to-end real above the delivery seams: the REAL
``/voice join`` (``_handle_voice_channel_join``), the REAL voice ingress
(``_handle_voice_channel_input`` → ``handle_message`` → the wrap → the
fan-out driver), the REAL member dispatch and the REAL
``_process_message_background`` auto-TTS block. Only the platform I/O
seams are faked (instance-level, mirroring the attribute names the code
reads): ``send``/``send_voice``/``play_in_voice_channel``/typing, the two
client calls a join needs, and the TTS tool.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from concierge import registry
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType

from tests.gateway.conftest import _ensure_discord_mock

_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter

_GUILD, _SPEAKER, _CHANNEL = 1, 42, 700
_MEMBER_A = "bot-reminders-a1b2"
_MEMBER_B = "bot-researcher-b2c3"


@pytest.fixture
def lane_rig(tmp_path, monkeypatch):
    """Multiplexed hub gateway with concierge on, plus the voice attributes
    the runner's join/ingress read (the ingress suite's rig shape; the
    adapter is the REAL DiscordAdapter, so ``build_source``, the wrap, the
    fan-out and the auto-TTS delivery block are all live)."""
    root = tmp_path
    for name in (_MEMBER_A, _MEMBER_B):
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
    runner._VOICE_MODE_PATH = root / "voice_modes.json"

    adapter = object.__new__(DiscordAdapter)
    BasePlatformAdapter.__init__(
        adapter, PlatformConfig(enabled=True, extra={}), Platform.DISCORD)
    adapter.gateway_runner = runner
    adapter._client = None  # no discord client: guild/channel lookups degrade
    adapter._voice_text_channels = {}
    adapter._voice_sources = {}
    adapter._voice_clients = {}
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {}
    return SimpleNamespace(root=root, runner=runner, adapter=adapter)


def _wire_delivery_seams(rig, monkeypatch):
    """Fake the platform I/O seams below the handler (instance-level, names
    mirroring the attributes production reads) and record every delivery:
    ``played`` (VC playback), ``sent`` (final text), ``voice_sent`` (voice
    attachments — the no-VC fallback), ``tts_calls`` (synthesis texts)."""
    played, sent, voice_sent, tts_calls = [], [], [], []

    async def _send(**kwargs):
        sent.append(kwargs.get("content"))
        return SendResult(success=True, message_id=f"m{len(sent)}")

    async def _send_voice(**kwargs):
        voice_sent.append(kwargs.get("audio_path"))
        return SendResult(success=True, message_id=f"v{len(voice_sent)}")

    async def _play_in_vc(guild_id, audio_path):
        played.append((guild_id, audio_path))
        return True

    async def _get_user_voice_channel(guild_id, user_id):
        return SimpleNamespace(name="Car VC")

    async def _join_voice_channel(channel):
        return True

    monkeypatch.setattr(rig.adapter, "send", _send)
    monkeypatch.setattr(rig.adapter, "send_voice", _send_voice)
    monkeypatch.setattr(rig.adapter, "play_in_voice_channel", _play_in_vc)
    monkeypatch.setattr(
        rig.adapter, "get_user_voice_channel", _get_user_voice_channel)
    monkeypatch.setattr(rig.adapter, "join_voice_channel", _join_voice_channel)

    async def _send_typing(chat_id, metadata=None):
        return None

    async def _stop_typing(chat_id, metadata=None):
        return None

    monkeypatch.setattr(rig.adapter, "send_typing", _send_typing)
    monkeypatch.setattr(rig.adapter, "stop_typing", _stop_typing)

    def _tts(*, text, output_path=None):
        Path(output_path).write_bytes(b"fake-audio")
        tts_calls.append(text)
        return json.dumps({"success": True, "file_paths": [output_path]})

    from tools import tts_tool

    monkeypatch.setattr(tts_tool, "check_tts_requirements", lambda: True)
    monkeypatch.setattr(tts_tool, "text_to_speech_tool", _tts)
    return SimpleNamespace(
        played=played, sent=sent, voice_sent=voice_sent, tts_calls=tts_calls)


async def _voice_join(rig):
    """A REAL runner ``/voice join`` on the bound text channel: the join
    event's source is built by the adapter's ``build_source`` (a typed
    message in that channel) and ``_handle_voice_channel_join`` wires
    everything production wires — the guild→text-channel binding, the
    serialized voice source, voice mode ``all`` (mirrored into the
    adapter's auto-TTS enabled set, which is what ``_wants_auto_tts``
    reads), and the input/disconnect callbacks. The two discord.py client
    calls are the faked seams; the VC connection state a successful join
    leaves behind is set here."""
    join_source = rig.adapter.build_source(
        chat_id=str(_CHANNEL), chat_type="group", user_id=str(_SPEAKER),
        user_name="Operator", guild_id=str(_GUILD))
    event = MessageEvent(
        source=join_source, text="/voice join",
        raw_message=SimpleNamespace(guild_id=_GUILD, guild=None))
    await rig.runner._handle_voice_channel_join(event)
    # the join's landed state — the exact assertions a live join guarantees
    assert rig.adapter._voice_text_channels.get(_GUILD) == _CHANNEL
    assert str(_CHANNEL) in rig.adapter._auto_tts_enabled_chats
    rig.adapter._voice_clients[_GUILD] = SimpleNamespace(is_connected=lambda: True)


async def _install_lane_runner(rig, responses, verdicts=None, before_reply=None):
    """Install the lane's TURN stand-in as the stored runner handler (the
    wrap delegates to it). It mirrors the production turn's voice-reply
    decision (run_turn_persistence._hmwa_deliver_turn_response): the REAL
    ``_should_send_voice_reply`` on the REAL joined state and the REAL
    ``_send_voice_reply`` when it fires — so the runner path is ARMED in
    every scenario here. The no-double-speak contract is that it stays
    silent for spoken lane turns; if it fired, its synthesis + playback
    would double the recorded counts. The stand-in pins
    ``already_sent=False`` (the default) because stand-in turns never
    stream, and the streaming escape is asserted directly in test 2.
    ``verdicts`` receives
    ``(profile, event, should_speak)`` per turn; ``before_reply`` runs at
    turn start (the mid-turn VC-drop race)."""
    async def _lane_handler(event):
        if before_reply is not None:
            before_reply(event)
        profile = getattr(event.source, "profile", None)
        response = responses.get(profile, "hub answer")
        should = rig.runner._should_send_voice_reply(event, response, [])
        if verdicts is not None:
            verdicts.append((profile, event, should))
        if should:
            await rig.runner._send_voice_reply(event, response)
        return response

    rig.adapter.set_message_handler(_lane_handler)


async def _speak(rig, text="brief me on the day"):
    """One transcribed utterance through the REAL voice ingress (the
    ``/voice join`` copy path: the stored serialized source, the restamp
    hook, the synthetic VOICE event, the real ``handle_message``)."""
    await rig.runner._handle_voice_channel_input(
        _GUILD, _SPEAKER, text, adapter=rig.adapter)


async def _typed_room_trigger(rig):
    """A TYPED room message through the wrap seam (the routing suite's
    dispatch shape — the hub's text-ingress batching is not this suite's
    subject, the lane DELIVERY is; the member turns still run the full real
    chain)."""
    source = rig.adapter.build_source(
        chat_id=str(_CHANNEL), chat_type="group", user_id=str(_SPEAKER),
        user_name="Operator", guild_id=str(_GUILD))
    event = MessageEvent(text="plan my day", source=source)
    await rig.adapter._concierge_inbound_dispatch(event)
    task = getattr(rig.adapter, "_concierge_room_fanout_tasks", {}).get(
        str(_CHANNEL))
    if task is not None:
        await task


async def _drain(adapter, *, timeout: float = 5.0) -> None:
    """Run every task the scenario spawned (the trigger turn, the fan-out
    driver, the member turns) to completion — bounded, so a wedged path
    fails the test instead of hanging it."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        pending = [t for t in list(adapter._background_tasks)
                   if not t.done() and t is not asyncio.current_task()]
        if not pending:
            return
        await asyncio.gather(*pending)
    raise AssertionError("lane delivery tasks did not settle within "
                         f"{timeout}s")


# ── (1) THE lane contract: spoken member answers land in the VC ────────────


@pytest.mark.asyncio
async def test_spoken_room_trigger_speaks_each_member_answer_in_the_vc(
        lane_rig, monkeypatch):
    """A spoken utterance on a room channel (voice mode on, hub adapter in
    the VC) fans out, and every member lane's final answer is SPOKEN in the
    connected VC — synthesized from THAT member's answer, played in the
    guild's voice channel, in the registry's stored fan-out order — while
    the full text still lands in the room channel."""
    registry.create_room(str(_CHANNEL), [_MEMBER_A, _MEMBER_B])
    seams = _wire_delivery_seams(lane_rig, monkeypatch)
    await _voice_join(lane_rig)
    await _install_lane_runner(
        lane_rig, {_MEMBER_A: "answer from reminders",
                   _MEMBER_B: "answer from researcher"})

    await _speak(lane_rig)
    await _drain(lane_rig.adapter)

    # every member's answer was synthesized and played in the VC, in order
    assert seams.tts_calls == ["answer from reminders", "answer from researcher"]
    assert len(seams.played) == 2
    assert all(guild == _GUILD for guild, _ in seams.played)
    # ...and the full text still landed in the room channel
    assert seams.sent == ["answer from reminders", "answer from researcher"]


# ── (2) No double-speak: exactly one playback per lane turn ────────────────


@pytest.mark.asyncio
async def test_no_double_speak_one_playback_per_lane_turn(
        lane_rig, monkeypatch):
    """The dedup contract: for each member lane turn exactly ONE synthesis
    and ONE playback — the runner-side auto voice reply (armed in the
    scenario through the real ``_should_send_voice_reply``) declines every
    spoken lane event, because the base adapter's auto-TTS already handles
    voice input (run_voice.py:425). The hub's own spoken turn keeps the
    streaming escape (``already_sent``) — today's contract."""
    registry.create_room(str(_CHANNEL), [_MEMBER_A, _MEMBER_B])
    seams = _wire_delivery_seams(lane_rig, monkeypatch)
    await _voice_join(lane_rig)
    verdicts = []
    await _install_lane_runner(
        lane_rig, {_MEMBER_A: "answer a", _MEMBER_B: "answer b"}, verdicts)

    await _speak(lane_rig)
    await _drain(lane_rig.adapter)

    # one lane turn per member, each declined by the runner-side guard
    assert [profile for profile, _, _ in verdicts] == [_MEMBER_A, _MEMBER_B]
    assert all(should is False for _, _, should in verdicts)
    # ...so exactly one synthesis + one playback per lane turn
    assert len(seams.tts_calls) == 2
    assert len(seams.played) == 2

    # the guard itself, on the REAL captured member event shape: a spoken
    # lane event is declined even when asked directly
    member_event = next(ev for prof, ev, _ in verdicts if prof == _MEMBER_A)
    assert member_event.message_type == MessageType.VOICE
    assert lane_rig.runner._should_send_voice_reply(
        member_event, "answer a", []) is False

    # the hub's own spoken turn: declined too (the adapter auto-TTS path
    # speaks it), but the streaming escape stays available — when streaming
    # consumed the response the RUNNER must speak it
    hub_source = lane_rig.runner._voice_input_source(
        lane_rig.adapter, _GUILD, _SPEAKER, _CHANNEL)
    hub_event = MessageEvent(
        source=hub_source, text="q", message_type=MessageType.VOICE,
        raw_message=SimpleNamespace(guild_id=_GUILD, guild=None))
    assert lane_rig.runner._should_send_voice_reply(
        hub_event, "hub answer", []) is False
    assert lane_rig.runner._should_send_voice_reply(
        hub_event, "hub answer", [], already_sent=True) is True


# ── (3) The hub's own spoken turn keeps today's auto voice reply ───────────


@pytest.mark.asyncio
async def test_hub_lane_answer_keeps_today_auto_voice_reply(
        lane_rig, monkeypatch):
    """No room, no binding: a spoken utterance delegates 1:1 to the hub
    lane and the hub's answer keeps today's behavior — the SAME adapter-side
    auto-TTS path, one synthesis, one VC playback, text still sent. The
    lane machinery must not have changed the hub's delivery."""
    seams = _wire_delivery_seams(lane_rig, monkeypatch)
    await _voice_join(lane_rig)
    verdicts = []
    await _install_lane_runner(lane_rig, {}, verdicts)

    await _speak(lane_rig)
    await _drain(lane_rig.adapter)

    assert [profile for profile, _, _ in verdicts] == [None]  # the hub lane
    assert seams.tts_calls == ["hub answer"]
    assert len(seams.played) == 1
    assert seams.played[0][0] == _GUILD
    assert seams.sent == ["hub answer"]


# ── (4a) Fail-closed / zero cost: voice session off ────────────────────────


@pytest.mark.asyncio
async def test_voice_mode_off_means_zero_tts_cost_text_only(
        lane_rig, monkeypatch):
    """THE zero-cost contract: with the voice session off (``/voice off``,
    leave, timeout — the state every no-VC transition leaves behind: no
    ``/voice join``, no enabled chat, no auto-TTS default) a room fan-out
    synthesizes NOTHING — no TTS tool call at all, no playback attempt, the
    members' answers arrive as text only."""
    registry.create_room(str(_CHANNEL), [_MEMBER_A, _MEMBER_B])
    seams = _wire_delivery_seams(lane_rig, monkeypatch)
    # no _voice_join: the adapter holds no voice binding, the runner's
    # voice-mode store is empty, _auto_tts_default is False
    assert lane_rig.adapter._auto_tts_default is False
    await _install_lane_runner(
        lane_rig, {_MEMBER_A: "answer a", _MEMBER_B: "answer b"})

    await _typed_room_trigger(lane_rig)
    await _drain(lane_rig.adapter)

    assert seams.tts_calls == []      # zero synthesis — no cost when unused
    assert seams.played == []          # no playback
    assert seams.voice_sent == []      # no voice attachments either
    assert seams.sent == ["answer a", "answer b"]  # text-only delivery


# ── (4b) The no-VC race: designed attachment fallback, never VC playback ──


@pytest.mark.asyncio
async def test_vc_drop_mid_turn_falls_back_to_voice_attachment(
        lane_rig, monkeypatch):
    """The other no-VC case, pinned as what the code actually does: the VC
    connection drops between the spoken trigger and the reply (mode still
    on). ``play_tts``'s designed fallback (adapter.py:3701-3708) applies —
    NO VC playback, the audio still reaches the user as a voice attachment
    in the linked text channel, still exactly one synthesis per lane turn
    and the full text behind it. Destroying this fallback to force
    text-only would take the answer away from a driving user."""
    registry.create_room(str(_CHANNEL), [_MEMBER_A, _MEMBER_B])
    seams = _wire_delivery_seams(lane_rig, monkeypatch)
    await _voice_join(lane_rig)
    # the VC drops while the lane turn runs (before its reply is produced)
    def _drop_vc(event):
        lane_rig.adapter._voice_clients.clear()
    await _install_lane_runner(
        lane_rig, {_MEMBER_A: "answer a", _MEMBER_B: "answer b"},
        before_reply=_drop_vc)

    await _speak(lane_rig)
    await _drain(lane_rig.adapter)

    assert seams.played == []          # no VC playback without a connection
    assert len(seams.voice_sent) == 2  # the designed fallback: attachments
    assert len(seams.tts_calls) == 2   # still one synthesis per lane turn
    assert seams.sent == ["answer a", "answer b"]
