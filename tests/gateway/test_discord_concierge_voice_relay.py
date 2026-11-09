"""Concierge voice relay: with ``concierge.voice_relay.mode: summary``, a
spoken room trigger's member-lane answers are SPOKEN as short auxiliary-LLM
summaries — the full text still lands in the room, and the playback path is
exactly the ONE the Task 2 lane-delivery suite pinned (single synthesis,
single VC playback per lane turn).

The seam this suite pins: the base auto-TTS block calls
``self._wants_auto_tts(...)`` (base.py:4124) and, immediately after,
``await self._synthesize_auto_tts(text_content)`` (base.py:4507) — two
``self`` methods the adapter MRO resolves onto ``ConciergeVoiceMixin``.
The relay substitutes the synthesized TEXT at that seam for member-lane
voice events (the fan-out driver's plain-attr relay stamp marks them):
``_wants_auto_tts`` hands the event to the very next call on the same task
(no await between the two — a concurrent turn can never see another
turn's stash), and ``_synthesize_auto_tts`` swaps in the auxiliary summary
before delegating to the base synthesis. No second speaker exists: the
single-playback and dedup contracts hold by construction.

Contracts (per the Task 3 brief):
- summary mode: each member's answer is synthesized as the aux summary
  (task ``voice_relay`` through ``agent.auxiliary_client.async_call_llm``
  — observed at the machinery's entry point, never by source-reading),
  played once in the VC, while the room's text channel still receives the
  FULL answer;
- answers at/below ``max_chars`` skip the aux call entirely and speak
  verbatim;
- mode off / absent / verbatim: no aux call, verbatim synthesis —
  byte-identical to the pre-relay behavior (the Task 2 suite, which runs
  with no relay config, is the stronger unchanged contract);
- fail-closed: aux error or empty summary -> verbatim synthesis (audio
  still arrives) + ONE warning; TTS failure -> today's auto-TTS posture
  (warning, full text unaffected).

Rig shape: the lane-delivery suite's (real /voice join, real voice ingress,
real fan-out, real base delivery block; only the platform I/O and the aux
machinery's entry point are faked).
"""

from __future__ import annotations

import asyncio
import json
import logging
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
from gateway.platforms.event import MessageEvent

from tests.gateway.conftest import _ensure_discord_mock

_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter

_GUILD, _SPEAKER, _CHANNEL = 1, 42, 700
_MEMBER_A = "bot-reminders-a1b2"
_MEMBER_B = "bot-researcher-b2c3"
# Long enough to exceed the 400-char max_chars default in every scenario.
_LONG_ANSWER_A = ("You have a busy day ahead with three meetings and a review. " * 8).strip()
_LONG_ANSWER_B = ("Markets are mixed with tech up and energy down this morning. " * 8).strip()
_SUMMARY_A = "Reminders: three meetings today plus one review."
_SUMMARY_B = "Research: markets are mixed, tech is up, energy is down."


@pytest.fixture
def lane_rig(tmp_path, monkeypatch):
    """Multiplexed hub gateway with concierge on (the lane-delivery rig
    shape); the relay config is set per test on the in-hand ``concierge``
    section, which is what the seam reads at dispatch time."""
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


def _relay_config(rig, mode=None, max_chars=None):
    """Set (or clear) ``concierge.voice_relay`` on the in-hand config."""
    if mode is None:
        rig.runner.config.concierge.pop("voice_relay", None)
        return
    relay = {"mode": mode}
    if max_chars is not None:
        relay["max_chars"] = max_chars
    rig.runner.config.concierge["voice_relay"] = relay


def _wire_delivery_seams(rig, monkeypatch, tts=None):
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
        if tts is not None:
            tts(text)
        Path(output_path).write_bytes(b"fake-audio")
        tts_calls.append(text)
        return json.dumps({"success": True, "file_paths": [output_path]})

    from tools import tts_tool

    monkeypatch.setattr(tts_tool, "check_tts_requirements", lambda: True)
    monkeypatch.setattr(tts_tool, "text_to_speech_tool", _tts)
    return SimpleNamespace(
        played=played, sent=sent, voice_sent=voice_sent, tts_calls=tts_calls)


def _wire_aux(monkeypatch, summaries, error=None, messages=None):
    """Fake the auxiliary machinery's ENTRY POINT (``async_call_llm``) and
    record every call's task + messages — the observable proof that the
    relay's summary rides the auxiliary machinery as task ``voice_relay``
    (pinnable via ``auxiliary.voice_relay``), never a member profile's main
    model call path. Production imports the function at call time, so the
    patched module attribute is what the relay resolves.

    ``summaries`` are wrapped as plain content messages; ``messages`` (when
    given) are bare message objects (content/reasoning shapes) popped in
    call order — the REAL ``extract_content_or_reasoning`` downstream
    decides what a reasoning-only response yields."""
    calls = []
    responses = list(summaries)
    pending = list(messages or [])

    async def _fake_async_call_llm(task=None, *, messages=None, **kwargs):
        calls.append(SimpleNamespace(task=task, messages=messages, kwargs=kwargs))
        if error is not None:
            raise error
        if pending:
            message = pending.pop(0)
        else:
            content = responses.pop(0) if responses else ""
            message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    from agent import auxiliary_client

    monkeypatch.setattr(auxiliary_client, "async_call_llm", _fake_async_call_llm)
    return calls


async def _voice_join(rig):
    """A REAL runner ``/voice join`` on the bound text channel (the
    lane-delivery rig's join: everything production wires is wired — the
    guild->text-channel binding, the serialized voice source, voice mode,
    the auto-TTS enabled chat set, the input callbacks)."""
    join_source = rig.adapter.build_source(
        chat_id=str(_CHANNEL), chat_type="group", user_id=str(_SPEAKER),
        user_name="Operator", guild_id=str(_GUILD))
    event = MessageEvent(
        source=join_source, text="/voice join",
        raw_message=SimpleNamespace(guild_id=_GUILD, guild=None))
    await rig.runner._handle_voice_channel_join(event)
    assert rig.adapter._voice_text_channels.get(_GUILD) == _CHANNEL
    assert str(_CHANNEL) in rig.adapter._auto_tts_enabled_chats
    rig.adapter._voice_clients[_GUILD] = SimpleNamespace(is_connected=lambda: True)


async def _install_lane_runner(rig, responses):
    """Install the lane's TURN stand-in as the stored runner handler (the
    wrap delegates to it). It mirrors the production turn's voice-reply
    decision (run_turn_persistence._hmwa_deliver_turn_response): the REAL
    ``_should_send_voice_reply`` on the REAL joined state and the REAL
    ``_send_voice_reply`` when it fires — so the runner path is ARMED in
    every scenario here; the no-double-speak contract is that it stays
    silent for spoken lane turns. The stand-in pins ``already_sent=False``
    (the default) because stand-in turns never stream; the streaming escape
    itself is asserted directly in the Task 2 suite."""
    async def _lane_handler(event):
        profile = getattr(event.source, "profile", None)
        return responses.get(profile, "hub answer")

    rig.adapter.set_message_handler(_lane_handler)


async def _speak(rig, text="brief me on the day"):
    """One transcribed utterance through the REAL voice ingress (the
    ``/voice join`` copy path: the stored serialized source, the restamp
    hook, the synthetic VOICE event, the real ``handle_message``)."""
    await rig.runner._handle_voice_channel_input(
        _GUILD, _SPEAKER, text, adapter=rig.adapter)


async def _drain(adapter, *, timeout: float = 5.0) -> None:
    """Run every task the scenario spawned to completion — bounded, so a
    wedged path fails the test instead of hanging it."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        pending = [t for t in list(adapter._background_tasks)
                   if not t.done() and t is not asyncio.current_task()]
        if not pending:
            return
        await asyncio.gather(*pending)
    raise AssertionError("relay tasks did not settle within "
                         f"{timeout}s")


# ── (1) Summary mode: the aux summary is spoken, the full text lands ──────


@pytest.mark.asyncio
async def test_summary_mode_speaks_aux_summary_text_stays_full(
        lane_rig, monkeypatch):
    """THE relay contract: with ``mode: summary`` each member lane's answer
    over ``max_chars`` is SPOKEN as the auxiliary-LLM summary — synthesized
    from the summary text, played once per lane turn in the VC, in fan-out
    order — while the room's text channel still receives the FULL answer.
    The summary call rides the auxiliary machinery: recorded at
    ``async_call_llm`` with task ``voice_relay`` (per-task pinnable via
    ``auxiliary.voice_relay``) and the FULL answer in the prompt."""
    registry.create_room(str(_CHANNEL), [_MEMBER_A, _MEMBER_B])
    _relay_config(lane_rig, mode="summary", max_chars=400)
    seams = _wire_delivery_seams(lane_rig, monkeypatch)
    aux_calls = _wire_aux(monkeypatch, [_SUMMARY_A, _SUMMARY_B])
    await _voice_join(lane_rig)
    await _install_lane_runner(
        lane_rig, {_MEMBER_A: _LONG_ANSWER_A, _MEMBER_B: _LONG_ANSWER_B})

    await _speak(lane_rig)
    await _drain(lane_rig.adapter)

    # one aux call per member lane turn, through the machinery, task-pinned
    assert len(aux_calls) == 2
    assert all(call.task == "voice_relay" for call in aux_calls)
    assert any(_LONG_ANSWER_A in str(m) for m in aux_calls[0].messages)
    assert any(_LONG_ANSWER_B in str(m) for m in aux_calls[1].messages)
    # the SYNTHESIZED text is the summary, not the full answer...
    assert seams.tts_calls == [_SUMMARY_A, _SUMMARY_B]
    # ...played once per lane turn in the VC...
    assert len(seams.played) == 2
    assert all(guild == _GUILD for guild, _ in seams.played)
    # ...while the room still receives the FULL answers
    assert seams.sent == [_LONG_ANSWER_A, _LONG_ANSWER_B]


# ── (2) Short answers skip the aux call and speak verbatim ─────────────────


@pytest.mark.asyncio
async def test_short_answers_skip_aux_call_and_speak_verbatim(
        lane_rig, monkeypatch):
    """``max_chars`` short-circuit: answers at/below the cap are spoken
    VERBATIM with no auxiliary call at all — the relay must never add an
    LLM round-trip (cost, latency) to an answer that is already
    driving-appropriate."""
    registry.create_room(str(_CHANNEL), [_MEMBER_A, _MEMBER_B])
    _relay_config(lane_rig, mode="summary", max_chars=400)
    seams = _wire_delivery_seams(lane_rig, monkeypatch)
    aux_calls = _wire_aux(monkeypatch, [])
    await _voice_join(lane_rig)
    await _install_lane_runner(
        lane_rig, {_MEMBER_A: "answer a", _MEMBER_B: "answer b"})

    await _speak(lane_rig)
    await _drain(lane_rig.adapter)

    assert aux_calls == []                       # no LLM call for short answers
    assert seams.tts_calls == ["answer a", "answer b"]  # verbatim synthesis
    assert len(seams.played) == 2
    assert seams.sent == ["answer a", "answer b"]


# ── (3) off / absent / verbatim: no aux call, byte-identical behavior ─────


@pytest.mark.asyncio
@pytest.mark.parametrize("relay", [
    pytest.param(None, id="absent"),
    pytest.param({"mode": "off"}, id="off"),
    pytest.param({"mode": "verbatim"}, id="verbatim"),
    pytest.param({"mode": "summary", "max_chars": 10_000}, id="cap-covers"),
])
async def test_relay_disabled_variants_speak_verbatim_no_aux(
        lane_rig, monkeypatch, relay):
    """Zero-behavior-change contracts: with the key absent, mode ``off``,
    mode ``verbatim``, or a ``max_chars`` cap that covers the answers, a
    spoken fan-out synthesizes the FULL answers verbatim and makes NO
    auxiliary call — exactly the pre-relay delivery the Task 2 suite pins.
    An unknown ``mode`` value fails closed the same way (covered by the
    settings reader's allow-list, exercised via the absent case)."""
    registry.create_room(str(_CHANNEL), [_MEMBER_A, _MEMBER_B])
    if relay is None:
        _relay_config(lane_rig)
    else:
        lane_rig.runner.config.concierge["voice_relay"] = relay
    seams = _wire_delivery_seams(lane_rig, monkeypatch)
    aux_calls = _wire_aux(monkeypatch, [])
    await _voice_join(lane_rig)
    await _install_lane_runner(
        lane_rig, {_MEMBER_A: _LONG_ANSWER_A, _MEMBER_B: _LONG_ANSWER_B})

    await _speak(lane_rig)
    await _drain(lane_rig.adapter)

    assert aux_calls == []
    assert seams.tts_calls == [_LONG_ANSWER_A, _LONG_ANSWER_B]
    assert len(seams.played) == 2
    assert seams.sent == [_LONG_ANSWER_A, _LONG_ANSWER_B]


# ── (4) Fail-closed: aux failure -> verbatim fallback + ONE warning ───────


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["error", "empty", "markdown-only"])
async def test_aux_failure_falls_back_to_verbatim_with_one_warning(
        lane_rig, monkeypatch, caplog, kind):
    """Fail-closed relay: an aux-summarizer failure (raised error, an
    empty summary, or a summary that is nothing but markdown and strips
    to nothing — the empty check must see the text that would actually be
    SPOKEN) degrades to VERBATIM synthesis — audio still arrives, exactly
    once, and the full text still lands — and logs exactly ONE warning
    per member. A relay bug must never break delivery."""
    member = f"bot-relay-{kind}-x1"
    (lane_rig.root / "profiles" / member).mkdir(parents=True)
    (lane_rig.root / "profiles" / member / ".env").write_text("OPENAI_API_KEY=sk-x\n")
    registry.create_room(str(_CHANNEL), [member])
    _relay_config(lane_rig, mode="summary", max_chars=400)
    seams = _wire_delivery_seams(lane_rig, monkeypatch)
    error = RuntimeError("aux provider down") if kind == "error" else None
    summaries = [""] if kind == "empty" else ["___"]
    aux_calls = _wire_aux(monkeypatch, summaries, error=error)
    await _voice_join(lane_rig)
    await _install_lane_runner(lane_rig, {member: _LONG_ANSWER_A})

    with caplog.at_level(logging.WARNING):
        await _speak(lane_rig)
        await _drain(lane_rig.adapter)

    assert len(aux_calls) == 1                    # the relay tried
    assert seams.tts_calls == [_LONG_ANSWER_A]    # verbatim fallback
    assert len(seams.played) == 1                 # audio still arrives, once
    assert seams.sent == [_LONG_ANSWER_A]          # full text unaffected
    relay_warnings = [r for r in caplog.records
                      if r.levelno == logging.WARNING and "voice relay" in r.getMessage()]
    assert len(relay_warnings) == 1


# ── (5) TTS failure: today's auto-TTS posture, text unaffected ─────────────


@pytest.mark.asyncio
async def test_tts_failure_leaves_text_delivery_unaffected(
        lane_rig, monkeypatch, caplog):
    """The relay's own failure boundary ends at the synthesis text: when
    the TTS tool itself fails downstream, today's auto-TTS posture applies
    (warning, no playback, no attachments) and the lane's FULL text is still
    delivered to the room — the relay neither amplifies nor masks a TTS
    failure."""
    registry.create_room(str(_CHANNEL), [_MEMBER_A])
    _relay_config(lane_rig, mode="summary", max_chars=400)
    raised = []

    def _boom(text):
        raised.append(text)
        raise RuntimeError("tts engine down")

    seams = _wire_delivery_seams(lane_rig, monkeypatch, tts=_boom)
    aux_calls = _wire_aux(monkeypatch, [_SUMMARY_A])
    await _voice_join(lane_rig)
    await _install_lane_runner(lane_rig, {_MEMBER_A: _LONG_ANSWER_A})

    with caplog.at_level(logging.WARNING):
        await _speak(lane_rig)
        await _drain(lane_rig.adapter)

    assert len(aux_calls) == 1            # the relay ran (summary requested)
    assert raised == [_SUMMARY_A]         # TTS was fed the SUMMARY, then failed
    assert seams.tts_calls == []          # no synthesis succeeded
    assert seams.played == []             # no playback
    assert seams.voice_sent == []         # no attachment fallback either
    assert seams.sent == [_LONG_ANSWER_A]  # the full text still lands


# ── (6) Settings resolution: garbage/non-positive caps and unknown modes ───


@pytest.mark.parametrize("relay, mode, max_chars", [
    pytest.param({"mode": "summary", "max_chars": -5}, "summary", 400,
                 id="negative-cap"),
    pytest.param({"mode": "summary", "max_chars": 0}, "summary", 400,
                 id="zero-cap"),
    pytest.param({"mode": "summary", "max_chars": "abc"}, "summary", 400,
                 id="garbage-cap"),
    pytest.param({"mode": "summary", "max_chars": None}, "summary", 400,
                 id="null-cap"),
    pytest.param({"mode": "summary"}, "summary", 400, id="cap-absent"),
    pytest.param({"mode": "bogus"}, "off", 400, id="unknown-mode"),
    pytest.param("not-a-dict", "off", 400, id="non-dict-block"),
])
def test_voice_relay_settings_resolve_bad_values_to_defaults(relay, mode,
                                                             max_chars):
    """The settings reader's fail-closed docstring, pinned: an unknown
    mode (allow-list), a non-dict block, and every garbage or NON-POSITIVE
    ``max_chars`` all resolve to the no-op/400-default posture — a typo'd
    ``max_chars: -5`` must never silently become summarize-everything."""
    from plugins.platforms.discord.concierge_voice import _voice_relay_settings

    section = {} if relay is None else {"voice_relay": relay}
    settings = _voice_relay_settings(
        SimpleNamespace(config=SimpleNamespace(concierge=section)))
    assert (settings.mode, settings.max_chars) == (mode, max_chars)


# ── (7) SPEECH never gets chain-of-thought: reasoning-only aux response ─────


@pytest.mark.asyncio
async def test_reasoning_only_aux_response_speaks_verbatim_not_cot(
        lane_rig, monkeypatch, caplog):
    """CoT is never appropriate for SPEECH: when the aux summarizer's
    token budget lands in reasoning (content empty, reasoning non-empty),
    the response is a FAILED summary — the relay rejects the reasoning
    fallback outright, warns, and speaks the member's answer VERBATIM
    (never the raw chain-of-thought in the car)."""
    member = "bot-relay-cot-x1"
    (lane_rig.root / "profiles" / member).mkdir(parents=True)
    (lane_rig.root / "profiles" / member / ".env").write_text("OPENAI_API_KEY=sk-x\n")
    registry.create_room(str(_CHANNEL), [member])
    _relay_config(lane_rig, mode="summary", max_chars=400)
    seams = _wire_delivery_seams(lane_rig, monkeypatch)
    cot = ("The user is driving and asked about meetings. I should consider "
           "the calendar carefully before answering with a summary.")
    aux_calls = _wire_aux(
        monkeypatch, [], messages=[SimpleNamespace(content="", reasoning=cot)])
    await _voice_join(lane_rig)
    await _install_lane_runner(lane_rig, {member: _LONG_ANSWER_A})

    with caplog.at_level(logging.WARNING):
        await _speak(lane_rig)
        await _drain(lane_rig.adapter)

    assert len(aux_calls) == 1                    # the relay tried the aux call
    assert seams.tts_calls == [_LONG_ANSWER_A]    # the VERBATIM answer is spoken
    assert all(cot not in spoken for spoken in seams.tts_calls)  # never the CoT
    assert len(seams.played) == 1                 # audio still arrives, once
    assert seams.sent == [_LONG_ANSWER_A]          # full text unaffected
    relay_warnings = [r for r in caplog.records
                      if r.levelno == logging.WARNING and "voice relay" in r.getMessage()]
    assert len(relay_warnings) == 1                # ...with the fail-closed warning


# ── (8) A disobedient model's markdown never reaches the synthesizer ───────


@pytest.mark.asyncio
async def test_markdown_in_summary_is_spoken_stripped(lane_rig, monkeypatch):
    """The spoken summary is stripped of markdown before synthesis: a
    disobedient summarizer's ``**bold**`` must not reach TTS — the
    synthesized text is the plain spoken form, and the room still gets the
    full answer."""
    registry.create_room(str(_CHANNEL), [_MEMBER_A])
    _relay_config(lane_rig, mode="summary", max_chars=400)
    seams = _wire_delivery_seams(lane_rig, monkeypatch)
    aux_calls = _wire_aux(
        monkeypatch, ["**Reminders**: three meetings today plus "
                       "one review."])
    await _voice_join(lane_rig)
    await _install_lane_runner(lane_rig, {_MEMBER_A: _LONG_ANSWER_A})

    await _speak(lane_rig)
    await _drain(lane_rig.adapter)

    assert len(aux_calls) == 1
    assert seams.tts_calls == ["Reminders: three meetings today plus one review."]
    assert len(seams.played) == 1
    assert seams.sent == [_LONG_ANSWER_A]


# ── (9) An aux outage is transient: every failing turn warns again ──────────


@pytest.mark.asyncio
async def test_transient_aux_failure_warns_every_turn(lane_rig, monkeypatch,
                                                      caplog):
    """The relay failure warning is a plain per-turn warning, not a
    warn-once keyed suppression: an aux outage is transient (not a durable
    registry state), so a NEW failure for the same member must never be
    silent forever — each failing member turn logs the fallback warning
    again (it is already once-per-turn by construction)."""
    member = "bot-relay-transient-x1"
    (lane_rig.root / "profiles" / member).mkdir(parents=True)
    (lane_rig.root / "profiles" / member / ".env").write_text("OPENAI_API_KEY=sk-x\n")
    registry.create_room(str(_CHANNEL), [member])
    _relay_config(lane_rig, mode="summary", max_chars=400)
    seams = _wire_delivery_seams(lane_rig, monkeypatch)
    aux_calls = _wire_aux(monkeypatch, [], error=RuntimeError("aux provider down"))
    await _voice_join(lane_rig)
    await _install_lane_runner(lane_rig, {member: _LONG_ANSWER_A})

    with caplog.at_level(logging.WARNING):
        await _speak(lane_rig)
        await _drain(lane_rig.adapter)
        # A different transcript: the runner's voice-transcript dedup
        # (12s window) would silently swallow a repeated utterance.
        await _speak(lane_rig, text="and what about the afternoon")
        await _drain(lane_rig.adapter)

    assert len(aux_calls) == 2                              # both turns tried
    assert seams.tts_calls == [_LONG_ANSWER_A, _LONG_ANSWER_A]  # verbatim both
    assert len(seams.played) == 2
    relay_warnings = [r for r in caplog.records
                      if r.levelno == logging.WARNING and "voice relay" in r.getMessage()]
    assert len(relay_warnings) == 2                         # one warning PER turn

