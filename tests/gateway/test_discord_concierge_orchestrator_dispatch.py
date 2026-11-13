"""Concierge orchestrator dispatch, T5 (PLAN decision 2): the Discord-mediated
dispatch capability on the runner-seam wrap.

The orchestrator's SOUL answers some requests with a single directive line —
``@dispatch <blueprint-key> <task text>`` — instead of doing the work itself.
The WRAP (``_concierge_inbound_dispatch``, the ``set_message_handler`` seam
every live dispatch funnels through) recognizes that response on a
REGISTRY-CONFIRMED orchestrator turn and spawns a background DISPATCH DRIVER
that does the whole exchange through the room machinery, visibly, in Discord:

1. the directive response is SUPPRESSED (the wrap returns ``None`` — the
   driver delivers everything);
2. the worker bot is provisioned lazily (channel-less, per-user, idempotent)
   and its channel created lazily + bound ONCE (the second dispatch reuses
   both);
3. a visible handoff (``→ asked <Title>: <task>``) lands in the home channel;
4. the worker's turn runs through the REAL base chain
   (``super().handle_message`` on a clone stamped with the worker profile,
   the worker channel as chat_id, and the TASK as the message text) with the
   home-channel window as context and NO voice-relay stamp — the worker's raw
   answer is never spoken;
5. the worker's reply is read back out of the worker channel's history window
   (the newest shared-identity MESSAGE — its author line through the next
   author line, so a multi-paragraph answer is captured WHOLE);
6. phase B: the ORCHESTRATOR summarizes — a second clone, stamped with the
   process-local dispatch-continuation marker (depth 1: a continuation turn
   never re-dispatches), carrying the ORIGINAL user text and the worker's
   reply as context. Its answer lands in the home channel through the normal
   delivery machinery — and on a VOICE event it is the ONE spoken playback.

Fail-closed ladder: every guard failure (bot author, non-orchestrator turn,
continuation stamp, non-string/shape-mismatched response, unknown or
``orchestrator`` blueprint key, unreadable registry) returns the response
UNCHANGED — the orchestrator answered alone. A PREPARE failure (registry
entry, worker provision, worker channel) warns once and posts ONE
best-effort ``⚠ dispatch failed`` notice into the home channel — the wrap
already suppressed the directive, so without the notice the user would see
nothing at all; a HANDOFF-post failure keeps the quiet abort (the handoff
post is the visible boundary, and a channel that could not take the handoff
could not take a notice). After the handoff, an explicit notice lands in the
home channel and phase B still runs — never a silent drop.

Rig: the room_live_chain suite's shape (a REAL DiscordAdapter over the
conftest discord mock, a REAL temp registry under an isolated HERMES_HOME, a
SPY as the runner handler set through ``set_message_handler``), plus the
voice-lane suite's delivery seams for the single-playback contract. Plain
marker classes only — a patched ``discord.Thread`` makes stubs Thread
instances (the §8 rig trap), and ``discord.MessageType.default`` is bound
ONCE at module scope (MagicMock enums mint a new member per access).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import socket
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from concierge import provision, registry
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType

from tests.gateway.conftest import _ensure_discord_mock

_ensure_discord_mock()

import discord
from plugins.platforms.discord import concierge_mixin
from plugins.platforms.discord.adapter import DiscordAdapter

# The §8 rig trap: MagicMock enums mint a NEW member per attribute access —
# bind the history formatter's message type ONCE, at module scope.
_MSG_TYPE_DEFAULT = discord.MessageType.default

GUILD = 42                    # the (fake) guild everything lives in
HOME = 555                    # the user's bound orchestrator home channel
CATEGORY = 777                # the home channel's (managed) category
OTHER_CHANNEL = 556           # a second bound channel, for the guard rows
USER = "1001"                 # the Discord author id
ORCH = "bot-orchestrator-x1"  # the author's orchestrator profile
BOUND_BOT = "bot-reminders-a1b2"  # a DIFFERENT bound bot (anti-spoof rows)
WORKER_KEY = "researcher"
WORKER_TITLE = "Researcher Bot"
WORKER_PROFILE = "bot-researcher-1001"  # what the fake provision mints
WORKER_CHANNEL = 700          # the lazily created worker channel
TASK = "find recent GLM papers"
DIRECTIVE = f"@dispatch {WORKER_KEY} {TASK}"
WORKER_ANSWER = "worker found 3 papers"
# a multi-paragraph answer (blank lines between the paragraphs) — the reply
# parser must capture it WHOLE, never just its first line
WORKER_ANSWER_MULTI = (
    "Paragraph one: the search ran clean."
    "\n\n"
    "Paragraph two: three papers surfaced, all recent."
    "\n\n"
    "Paragraph three: summaries follow."
)
PHASE_B_ANSWER = "summary: 3 papers found"
TRIGGER_TEXT = "please research GLM papers"

_HUMAN = SimpleNamespace(id=USER, bot=False, display_name="Operator")
_BOT = SimpleNamespace(id="9999", bot=True, display_name="Concierge Hub")
_ids = itertools.count(1000)


def _run(coro):
    """Run one scenario coroutine on a fresh loop (the room suite's shape)."""
    return asyncio.new_event_loop().run_until_complete(coro)


# ── The rig ─────────────────────────────────────────────────────────────────


class _FakeMessage:
    """One history message — a PLAIN marker class (never a patched
    discord.Thread), with the message type bound once at module scope."""

    def __init__(self, msg_id, content, author, channel=None):
        self.id = msg_id
        self.content = content
        self.clean_content = content
        self.author = author
        self.channel = channel
        self.type = _MSG_TYPE_DEFAULT
        self.attachments = None


class _FakeChannel:
    """A duck-typed Discord channel (plain marker class): ``send`` records
    the post AND lands it in the channel's own history as a shared-identity
    message, so the driver's window fetch sees exactly what the faked
    delivery lanes posted — the production shape (the bot's posted messages
    ARE the channel history)."""

    def __init__(self, channel_id, *, category=None):
        self.id = int(channel_id)
        self.category_id = int(category) if category else None
        self.messages: list = []
        self.sends: list = []

    async def send(self, text, **kwargs):
        self.sends.append(text)
        self.post(text)

    def post(self, content):
        self.messages.append(_FakeMessage(next(_ids), content, _BOT))

    async def history(self, *, limit=None, before=None, oldest_first=False):
        anchor = getattr(before, "id", None)
        msgs = [m for m in self.messages if anchor is None or m.id < anchor]
        if not oldest_first:
            msgs = list(reversed(msgs))
        cap = len(msgs) if limit is None else limit
        for msg in msgs[:cap]:
            yield msg


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """The room_live_chain rig shape (real adapter, real temp registry, live
    runner gates) PLUS the voice attributes the lane suite's ``/voice join``
    needs — one rig for every scenario below."""
    root = tmp_path
    for name in (ORCH, BOUND_BOT):
        (root / "profiles" / name).mkdir(parents=True)
        (root / "profiles" / name / ".env").write_text("OPENAI_API_KEY=sk-x\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    (root / "concierge").mkdir()

    # gateway/run.py imports hermes_bootstrap, whose module-level PM
    # activation stats the REAL ~/.hermes on a live-install checkout — stub
    # it BEFORE the import (the rig suites' shared workaround).
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

    channels = {str(HOME): _FakeChannel(HOME, category=CATEGORY),
                str(OTHER_CHANNEL): _FakeChannel(OTHER_CHANNEL)}
    guild = SimpleNamespace(
        id=GUILD,
        create_text_channel=AsyncMock(return_value=SimpleNamespace(id=WORKER_CHANNEL)))
    client = SimpleNamespace(
        user=_BOT, guilds=[guild],
        get_channel=lambda cid: channels.get(str(cid)),
        get_guild=lambda gid: guild if str(gid) == str(GUILD) else None)

    adapter = object.__new__(DiscordAdapter)
    BasePlatformAdapter.__init__(
        adapter, PlatformConfig(enabled=True, extra={}), Platform.DISCORD)
    adapter.gateway_runner = runner
    adapter._client = client
    adapter._voice_text_channels = {}
    adapter._voice_sources = {}
    adapter._voice_clients = {}
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {}

    return SimpleNamespace(root=root, runner=runner, adapter=adapter,
                           guild=guild, channels=channels)


def _seed(rig):
    """The post-bootstrap registry state: the user's orchestrator entry and
    the home channel's orchestrator binding (exactly what T4's bootstrap
    lands), plus the OTHER channel bound to a DIFFERENT bot."""
    registry.set_user(USER, orchestrator=ORCH, home_channel=str(HOME),
                      guild_id=str(GUILD))
    registry.bind_channel(str(HOME), profile=ORCH, blueprint="orchestrator",
                          guild_id=str(GUILD))
    registry.bind_channel(str(OTHER_CHANNEL), profile=BOUND_BOT,
                          blueprint="reminders", guild_id=str(GUILD))


def _fake_provision(rig, monkeypatch, calls):
    """The slow external, faked at the module the driver's lazy import
    resolves (the fallback suite's posture): a user-keyed, channel-less,
    idempotent stand-in that writes the profile dir, the profile.yaml title
    provisioning seeds, and the users-section worker record — the registry
    contract the real ``create_bot`` (T3) owns."""
    def _create_bot(blueprint_key, *, user_id=None, channel_id=None, **kwargs):
        calls.append((blueprint_key, user_id, channel_id))
        profile = f"bot-{blueprint_key}-{str(user_id)[-4:]}"
        pdir = rig.root / "profiles" / profile
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / ".env").write_text("OPENAI_API_KEY=sk-x\n")
        title = WORKER_TITLE if blueprint_key == WORKER_KEY else blueprint_key
        (pdir / "profile.yaml").write_text(f"description: {title}\n")
        registry.set_user_worker(user_id, blueprint_key, profile)
        return {"profile": profile, "blueprint": blueprint_key,
                "already_provisioned": False}

    monkeypatch.setattr(provision, "create_bot", _create_bot)


def _wire_io(rig, monkeypatch, *, tts=False):
    """Fake the platform I/O seams below the handler (the lane suite's
    attribute names) and record every delivery: ``sent`` (final text per
    chat), ``played`` (VC playback), ``tts_calls`` (synthesis texts)."""
    sent, played, tts_calls = [], [], []

    async def _send(**kwargs):
        chat_id = str(kwargs.get("chat_id"))
        content = kwargs.get("content")
        sent.append((chat_id, content))
        channel = rig.channels.get(chat_id)
        if channel is None:
            # the lazily created worker channel: the first delivery into it
            # materializes it (production: the created channel exists)
            channel = rig.channels[chat_id] = _FakeChannel(chat_id)
        if content:
            channel.post(content)  # a posted reply IS channel history
        return SendResult(success=True, message_id=str(next(_ids)))

    async def _send_voice(**kwargs):
        return SendResult(success=True, message_id=str(next(_ids)))

    async def _play_in_vc(guild_id, audio_path):
        played.append((guild_id, audio_path))
        return True

    async def _get_user_vc(guild_id, user_id):
        return SimpleNamespace(name="Car VC")

    async def _join_vc(channel):
        return True

    async def _typing(chat_id, metadata=None):
        return None

    async def _stop_typing(chat_id, metadata=None):
        return None

    monkeypatch.setattr(rig.adapter, "send", _send)
    monkeypatch.setattr(rig.adapter, "send_voice", _send_voice)
    monkeypatch.setattr(rig.adapter, "play_in_voice_channel", _play_in_vc)
    monkeypatch.setattr(rig.adapter, "get_user_voice_channel", _get_user_vc)
    monkeypatch.setattr(rig.adapter, "join_voice_channel", _join_vc)
    monkeypatch.setattr(rig.adapter, "send_typing", _typing)
    monkeypatch.setattr(rig.adapter, "stop_typing", _stop_typing)
    if tts:
        def _tts(*, text, output_path=None):
            Path(output_path).write_bytes(b"fake-audio")
            tts_calls.append(text)
            return json.dumps({"success": True, "file_paths": [output_path]})

        from tools import tts_tool

        monkeypatch.setattr(tts_tool, "check_tts_requirements", lambda: True)
        monkeypatch.setattr(tts_tool, "text_to_speech_tool", _tts)
    return SimpleNamespace(sent=sent, played=played, tts_calls=tts_calls)


def _install_handler(rig, calls, responses, *, phase_b=PHASE_B_ANSWER):
    """The runner-handler spy (installed through the LIVE wire path): answers
    per profile, with a distinct answer for phase-B continuation turns."""

    async def _handler(event):
        calls.append(event)
        if getattr(event, concierge_mixin._CONCIERGE_DISPATCH_ATTR, None):
            return responses.get("_phase_b", phase_b)
        return responses.get(getattr(event.source, "profile", None), "ok")

    rig.adapter.set_message_handler(_handler)


def _trigger(rig, *, text=TRIGGER_TEXT, chat_id=HOME,
             message_type=MessageType.TEXT):
    """A message in a BOUND channel, through the REAL ``build_source`` (the
    binding verdict stamps the orchestrator profile), carrying a raw message
    whose channel is the fake channel — the handoff post and the window
    fetches both need it."""
    channel = rig.channels[str(chat_id)]
    raw = _FakeMessage(next(_ids), text, _HUMAN, channel=channel)
    source = rig.adapter.build_source(
        chat_id=str(chat_id), chat_type="group", user_id=USER,
        user_name="Operator", guild_id=str(GUILD))
    return MessageEvent(text=text, source=source, raw_message=raw,
                        message_type=message_type, message_id=str(raw.id))


def _drive(rig, trigger, *, timeout=10.0):
    """One dispatch scenario: the trigger through the wrap seam, the driver
    awaited to completion, the turn tasks drained (the room suite's shape)."""

    async def _scenario():
        result = await rig.adapter._message_handler(trigger)
        task = (getattr(rig.adapter, "_concierge_room_fanout_tasks", None)
                or {}).get(str(getattr(trigger.source, "chat_id", "")))
        assert task is not None, "the dispatch driver was not spawned"
        await asyncio.wait_for(task, timeout=timeout)
        for t in list(rig.adapter._session_tasks.values()):
            if not t.done():
                await asyncio.wait_for(t, timeout=timeout)
        return result

    return _run(_scenario())


# ── (1) THE dispatch contract: the full two-phase exchange ──────────────────


def test_directive_dispatches_through_the_worker_and_summarizes(rig, monkeypatch):
    """A registry-confirmed orchestrator turn answering with the directive
    spawns the driver: the wrap returns None (the directive is suppressed),
    the worker is provisioned lazily (channel-less, user-keyed), its channel
    created lazily and bound once, the handoff names the blueprint TITLE, the
    worker turn runs with the TASK text on the worker profile (the home
    window as context, NO voice-relay stamp), and the stamped phase-B turn —
    the ORIGINAL user text plus the worker's reply as context — delivers the
    summary into the home channel through the normal machinery."""
    _seed(rig)
    provision_calls = []
    _fake_provision(rig, monkeypatch, provision_calls)
    seams = _wire_io(rig, monkeypatch)
    rig.channels[str(HOME)].messages.append(
        _FakeMessage(next(_ids), "earlier human line", _HUMAN))
    calls = []
    _install_handler(rig, calls, {ORCH: DIRECTIVE, WORKER_PROFILE: WORKER_ANSWER})

    result = _drive(rig, _trigger(rig))

    assert result is None  # the directive response is suppressed
    assert provision_calls == [(WORKER_KEY, USER, None)]
    assert rig.guild.create_text_channel.await_count == 1
    binding = registry.get_binding(str(WORKER_CHANNEL))
    assert binding["profile"] == WORKER_PROFILE
    assert binding["blueprint"] == WORKER_KEY
    assert rig.adapter._concierge_channel_is_hub_created(str(WORKER_CHANNEL))
    # the visible handoff: the blueprint TITLE, the task
    assert any(s == f"→ asked **{WORKER_TITLE}**: {TASK}"
               for s in rig.channels[str(HOME)].sends)
    # the worker turn: the TASK as text, the worker profile + channel, the
    # home window as context, and NO voice-relay stamp on the lane
    worker_call = next(c for c in calls
                       if getattr(c.source, "profile", None) == WORKER_PROFILE)
    assert worker_call.text == TASK
    assert worker_call.source.chat_id == str(WORKER_CHANNEL)
    assert worker_call is not calls[0]  # a fresh event, never the trigger
    assert getattr(worker_call, concierge_mixin._VOICE_RELAY_ATTR, None) is None
    assert "[Recent channel messages]" in (worker_call.channel_context or "")
    assert "earlier human line" in worker_call.channel_context
    # phase B: the stamped continuation, original text, orchestrator profile,
    # home channel, and BOTH the request context and the worker's reply
    phase_b = next(c for c in calls
                   if getattr(c, concierge_mixin._CONCIERGE_DISPATCH_ATTR, None))
    assert phase_b.text == TRIGGER_TEXT
    assert phase_b.source.profile == ORCH
    assert phase_b.source.chat_id == str(HOME)
    assert f"[{WORKER_TITLE} replied]" in phase_b.channel_context
    assert WORKER_ANSWER in phase_b.channel_context
    assert "earlier human line" in phase_b.channel_context
    # deliveries: the worker's answer into the worker channel (read back for
    # phase B), the summary into the home channel
    assert (str(WORKER_CHANNEL), WORKER_ANSWER) in seams.sent
    assert (str(HOME), PHASE_B_ANSWER) in seams.sent


def test_multi_line_worker_reply_reaches_phase_b_whole(rig, monkeypatch):
    """IMPORTANT #1 (quality review): a MULTI-PARAGRAPH worker answer (blank
    lines between the paragraphs) must reach the phase-B context block WHOLE —
    the worker's reply is the newest shared-identity MESSAGE of the worker
    channel's window (its author line through the NEXT author line or the
    window's end), not just its first line. The per-line parser summarized
    phase B from a truncated answer."""
    _seed(rig)
    provision_calls = []
    _fake_provision(rig, monkeypatch, provision_calls)
    _wire_io(rig, monkeypatch)
    calls = []
    _install_handler(
        rig, calls, {ORCH: DIRECTIVE, WORKER_PROFILE: WORKER_ANSWER_MULTI})

    _drive(rig, _trigger(rig))

    phase_b = next(c for c in calls
                   if getattr(c, concierge_mixin._CONCIERGE_DISPATCH_ATTR, None))
    # every line of the answer, in order, as one contiguous block
    assert f"[{WORKER_TITLE} replied]\n{WORKER_ANSWER_MULTI}" in (
        phase_b.channel_context or "")
    for line in WORKER_ANSWER_MULTI.splitlines():
        assert line in (phase_b.channel_context or "")


def test_second_dispatch_reuses_the_worker_and_channel(rig, monkeypatch):
    """Idempotence: the second directive reuses the provisioned worker and
    the bound channel — provision and channel creation run ONCE, and the
    second exchange completes just like the first."""
    _seed(rig)
    provision_calls = []
    _fake_provision(rig, monkeypatch, provision_calls)
    _wire_io(rig, monkeypatch)
    calls = []
    _install_handler(rig, calls, {ORCH: DIRECTIVE, WORKER_PROFILE: WORKER_ANSWER})

    _drive(rig, _trigger(rig))
    _drive(rig, _trigger(rig, text="more papers please"))

    assert len(provision_calls) == 1
    assert rig.guild.create_text_channel.await_count == 1
    stamped = [c for c in calls
               if getattr(c, concierge_mixin._CONCIERGE_DISPATCH_ATTR, None)]
    assert len(stamped) == 2  # both dispatches reached phase B


# ── (2) The guards: response UNCHANGED, no driver ──────────────────────────


def _no_driver(rig):
    assert (getattr(rig.adapter, "_concierge_room_fanout_tasks", None) or {}) == {}


def test_plain_response_is_returned_unchanged(rig, monkeypatch):
    """No directive, no dispatch: the most common case is byte-identical to
    the base behavior."""
    _seed(rig)
    provision_calls = []
    _fake_provision(rig, monkeypatch, provision_calls)
    calls = []
    _install_handler(rig, calls, {ORCH: "a plain answer"})

    trigger = _trigger(rig)
    result = _run(rig.adapter._message_handler(trigger))

    assert result == "a plain answer"
    assert calls == [trigger]
    assert provision_calls == []
    _no_driver(rig)


def test_non_orchestrator_turn_never_dispatches(rig, monkeypatch):
    """Anti-spoof: a directive-shaped response from a DIFFERENT bot's turn is
    returned unchanged — only a REGISTRY-CONFIRMED orchestrator turn (the
    profile equals the author's orchestrator) dispatches."""
    _seed(rig)
    provision_calls = []
    _fake_provision(rig, monkeypatch, provision_calls)
    calls = []
    _install_handler(rig, calls, {BOUND_BOT: DIRECTIVE})

    trigger = _trigger(rig, chat_id=OTHER_CHANNEL)
    result = _run(rig.adapter._message_handler(trigger))

    assert result == DIRECTIVE
    assert calls == [trigger]
    assert provision_calls == []
    _no_driver(rig)


def test_bot_authored_event_never_dispatches(rig, monkeypatch):
    """Anti-loop: the shared identity's own message (is_bot) never
    dispatches, whatever it says."""
    _seed(rig)
    calls = []
    _install_handler(rig, calls, {ORCH: DIRECTIVE})

    trigger = _trigger(rig)
    trigger.source.is_bot = True
    result = _run(rig.adapter._message_handler(trigger))

    assert result == DIRECTIVE
    assert calls == [trigger]
    _no_driver(rig)


def test_continuation_turn_never_redispatches(rig, monkeypatch):
    """Depth 1: an event carrying the dispatch-continuation stamp returns its
    response unchanged — even when the orchestrator answers the continuation
    with ANOTHER directive, the wrap never spawns a second driver."""
    _seed(rig)
    calls = []
    _install_handler(rig, calls, {ORCH: DIRECTIVE}, phase_b=DIRECTIVE)

    trigger = _trigger(rig)
    setattr(trigger, concierge_mixin._CONCIERGE_DISPATCH_ATTR, True)
    result = _run(rig.adapter._message_handler(trigger))

    assert result == DIRECTIVE
    assert calls == [trigger]
    _no_driver(rig)


def test_unknown_or_orchestrator_keys_do_not_dispatch(rig, monkeypatch):
    """Only a REAL worker blueprint key dispatches: an unknown key, and the
    orchestrator's own key (never a self-dispatch), return unchanged."""
    _seed(rig)
    provision_calls = []
    _fake_provision(rig, monkeypatch, provision_calls)
    responses = {ORCH: "@dispatch nosuchkey do things"}
    calls = []
    _install_handler(rig, calls, responses)

    for response in ("@dispatch nosuchkey do things",
                     "@dispatch orchestrator do things"):
        responses[ORCH] = response
        trigger = _trigger(rig)
        result = _run(rig.adapter._message_handler(trigger))
        assert result == response
        assert calls[-1] is trigger

    assert provision_calls == []
    _no_driver(rig)


def test_non_string_or_shape_mismatched_responses_are_unchanged(rig, monkeypatch):
    """Media objects, ephemeral wrappers, empty and near-miss strings are
    returned untouched — the guard only fires on a non-empty plain string
    whose first line is the directive."""
    _seed(rig)
    responses = {ORCH: None}
    calls = []
    _install_handler(rig, calls, responses)

    for response in (None, "", {"text": "a media shape"}, "@dispatch",
                     "not a directive", "  @dispatch researcher  "):
        responses[ORCH] = response
        trigger = _trigger(rig)
        result = _run(rig.adapter._message_handler(trigger))
        assert result == response
        assert calls[-1] is trigger

    _no_driver(rig)


def test_driver_spawn_failure_returns_the_directive_unchanged(rig, monkeypatch):
    """FAIL-CLOSED on the spawn side: if the driver cannot be tracked, the
    half-spawned task is CANCELLED (never run twice) and the orchestrator's
    directive answer is delivered as-is."""
    _seed(rig)
    calls = []
    _install_handler(rig, calls, {ORCH: DIRECTIVE})

    def _boom(channel_id, task):
        raise RuntimeError("tracking exploded")

    monkeypatch.setattr(rig.adapter, "_concierge_track_fanout_task", _boom)

    async def _scenario():
        result = await rig.adapter._message_handler(_trigger(rig))
        for _ in range(10):
            await asyncio.sleep(0)  # let the orphan's cancellation land
        return result

    result = _run(_scenario())

    assert result == DIRECTIVE
    assert len(calls) == 1  # only the trigger — no lane turn ever dispatched
    _no_driver(rig)


# ── (3) The fail-closed ladder after the guards ─────────────────────────────


def test_prepare_failure_posts_dispatch_failed_notice(rig, monkeypatch):
    """Ride-along (quality review): the wrap already SUPPRESSED the directive,
    so a PREPARE failure (registry entry, worker provisioning, worker channel)
    would otherwise leave the user with NOTHING — exactly ONE best-effort
    ``⚠ dispatch failed`` notice lands in the home channel and the driver
    stops (no handoff, no worker turn, no phase B). The HANDOFF subcase keeps
    its quiet abort (the next test): the handoff post is the visible
    boundary."""
    _seed(rig)

    def _provision_boom(*args, **kwargs):
        raise RuntimeError("worker provisioning blew up")

    monkeypatch.setattr(provision, "create_bot", _provision_boom)
    _wire_io(rig, monkeypatch)
    calls = []
    _install_handler(rig, calls, {ORCH: DIRECTIVE, WORKER_PROFILE: WORKER_ANSWER})

    _drive(rig, _trigger(rig))

    # exactly one notice in the home channel — and nothing else posted
    assert rig.channels[str(HOME)].sends == ["⚠ dispatch failed"]
    # the trigger reached the runner handler; no lane turn ever dispatched
    assert [getattr(c.source, "profile", None) for c in calls] == [ORCH]
    _no_driver(rig)


def test_worker_turn_failure_posts_notice_and_phase_b_still_runs(rig, monkeypatch):
    """A worker turn that cannot even START (the dispatch/spawn path raises —
    the one failure shape the base's turn machinery does NOT swallow into an
    error reply) posts the unavailable notice into the home channel, and
    phase B still runs — with the failure noted in its context — so the
    user is never left with just the handoff line."""
    _seed(rig)
    provision_calls = []
    _fake_provision(rig, monkeypatch, provision_calls)
    seams = _wire_io(rig, monkeypatch)
    calls = []
    _install_handler(rig, calls, {ORCH: DIRECTIVE, WORKER_PROFILE: WORKER_ANSWER})

    original_key = rig.adapter._event_session_key

    def _key(event):
        if getattr(event.source, "profile", None) == WORKER_PROFILE:
            raise RuntimeError("the worker turn spawn blew up")
        return original_key(event)

    monkeypatch.setattr(rig.adapter, "_event_session_key", _key)

    _drive(rig, _trigger(rig))

    assert any(f"⚠ **{WORKER_TITLE}** is unavailable" in s
               for s in rig.channels[str(HOME)].sends)
    # the worker turn never reached the runner handler
    assert all(getattr(c.source, "profile", None) != WORKER_PROFILE
               for c in calls)
    phase_b = next(c for c in calls
                   if getattr(c, concierge_mixin._CONCIERGE_DISPATCH_ATTR, None))
    assert "unavailable" in phase_b.channel_context
    assert (str(HOME), PHASE_B_ANSWER) in seams.sent


def test_missing_worker_reply_treated_as_failure(rig, monkeypatch):
    """A worker turn that answers NOTHING (no post to read back) is the same
    failure: notice + phase B with the failure noted."""
    _seed(rig)
    _fake_provision(rig, monkeypatch, [])
    seams = _wire_io(rig, monkeypatch)
    calls = []
    _install_handler(rig, calls, {ORCH: DIRECTIVE, WORKER_PROFILE: None})

    _drive(rig, _trigger(rig))

    assert any(f"⚠ **{WORKER_TITLE}** is unavailable" in s
               for s in rig.channels[str(HOME)].sends)
    phase_b = next(c for c in calls
                   if getattr(c, concierge_mixin._CONCIERGE_DISPATCH_ATTR, None))
    assert "unavailable" in phase_b.channel_context
    assert (str(HOME), PHASE_B_ANSWER) in seams.sent


def test_handoff_post_failure_aborts_the_driver_quietly(rig, monkeypatch):
    """The handoff is the point of no return: if it cannot be posted, the
    driver aborts with the warn-once and NOTHING else happens — no worker
    turn, no phase B, no notices."""
    _seed(rig)
    provision_calls = []
    _fake_provision(rig, monkeypatch, provision_calls)
    _wire_io(rig, monkeypatch)
    calls = []
    _install_handler(rig, calls, {ORCH: DIRECTIVE, WORKER_PROFILE: WORKER_ANSWER})

    class _BrokenChannel(_FakeChannel):
        async def send(self, text, **kwargs):
            raise RuntimeError("discord is down")

    rig.channels[str(HOME)] = _BrokenChannel(HOME, category=CATEGORY)
    broken_trigger = _trigger(rig)

    async def _scenario():
        result = await rig.adapter._message_handler(broken_trigger)
        task = (getattr(rig.adapter, "_concierge_room_fanout_tasks", None)
                or {}).get(str(HOME))
        if task is not None:
            await asyncio.wait_for(task, timeout=10.0)
        for t in list(rig.adapter._session_tasks.values()):
            if not t.done():
                await asyncio.wait_for(t, timeout=10.0)
        return result

    result = _run(_scenario())

    assert result is None  # the wrap suppressed the directive
    # the trigger reached the runner handler; no lane turn ever dispatched
    assert [getattr(c.source, "profile", None) for c in calls] == [ORCH]
    assert (getattr(rig.adapter, "_concierge_room_fanout_tasks", None) or {}) == {}
    assert rig.channels[str(HOME)].sends == []  # nothing else was posted


# ── (4) Voice: the phase-B answer is the ONE playback ───────────────────────


def test_voice_event_plays_back_exactly_once(rig, monkeypatch):
    """A SPOKEN directive on a voice-joined home channel: the worker lane
    (chat-gated off in the worker channel, and never relay-stamped) never
    synthesizes, and the phase-B summary is the ONE synthesis + playback —
    the full text still lands in both channels."""
    _seed(rig)
    provision_calls = []
    _fake_provision(rig, monkeypatch, provision_calls)
    seams = _wire_io(rig, monkeypatch, tts=True)
    calls = []
    _install_handler(rig, calls, {ORCH: DIRECTIVE, WORKER_PROFILE: WORKER_ANSWER})

    # a REAL runner /voice join on the home channel (the lane suite's shape)
    async def _join():
        join_source = rig.adapter.build_source(
            chat_id=str(HOME), chat_type="group", user_id=USER,
            user_name="Operator", guild_id=str(GUILD))
        await rig.runner._handle_voice_channel_join(MessageEvent(
            source=join_source, text="/voice join",
            raw_message=SimpleNamespace(guild_id=GUILD, guild=None)))

    _run(_join())
    assert str(HOME) in rig.adapter._auto_tts_enabled_chats
    rig.adapter._voice_clients[GUILD] = SimpleNamespace(is_connected=lambda: True)

    _drive(rig, _trigger(rig, message_type=MessageType.VOICE))

    assert seams.tts_calls == [PHASE_B_ANSWER]  # ONE playback: the summary
    assert len(seams.played) == 1              # in the guild's VC
    assert all(guild == GUILD for guild, _ in seams.played)
    # the worker lane never synthesized — its answer arrived as text
    assert (str(WORKER_CHANNEL), WORKER_ANSWER) in seams.sent
    assert (str(HOME), PHASE_B_ANSWER) in seams.sent


# ── (5) Cancellation hygiene: the fan-out task-dict entry ───────────────────


def test_cancelled_queued_driver_releases_its_task_dict_entry(rig):
    """Ride-along (quality review): the wrap records the driver under the
    channel's fan-out key at SPAWN time (``_concierge_track_fanout_task``),
    so a driver CANCELLED while still QUEUED on the room lock (another
    dispatch holding it) must still remove its entry — the cleanup cannot
    live inside the lock body, where a queued driver never arrives. The
    current-task ownership check is preserved: only the driver that OWNS the
    entry removes it."""
    _seed(rig)
    trigger = _trigger(rig)

    async def _scenario():
        lock = rig.adapter._concierge_room_lock_for(str(HOME))
        await lock.acquire()  # a first driver "holds" the lock: ours queues
        driver = asyncio.create_task(
            rig.adapter._concierge_orchestrator_dispatch(
                trigger, TASK, WORKER_KEY))
        rig.adapter._concierge_track_fanout_task(str(HOME), driver)
        await asyncio.sleep(0)  # the driver starts and queues on the lock
        assert not driver.done()  # parked on the lock, not finished
        driver.cancel()
        try:
            await driver
        except asyncio.CancelledError:
            pass  # the expected exit: cancelled while queued
        tasks = (getattr(rig.adapter, "_concierge_room_fanout_tasks", None)
                 or {})
        assert tasks.get(str(HOME)) is not driver  # entry released, no leak
        lock.release()

    _run(_scenario())
