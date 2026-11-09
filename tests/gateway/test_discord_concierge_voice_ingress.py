"""Concierge voice ingress: spoken input routes through the concierge wrap.

The operator speaks in a Discord voice channel; ``/voice join`` linked that
guild's voice to a text channel, and ``GatewayVoiceMixin._handle_voice_channel_input``
synthesizes a VOICE ``MessageEvent`` for the transcript and dispatches it via
``adapter.handle_message`` — the SAME live chain a typed message takes
(``_start_session_processing`` → ``_process_message_background`` → the
``set_message_handler`` wrap, under concierge ``_concierge_inbound_dispatch``).

Contracts, asserted at the real seams (member dispatch observed at the
INSTALLED runner handler; no source-reading assertions):

- ROOM channel: the room-targets stamp reaches the wrap → the fan-out driver
  runs every member's turn sequentially; the hub lane never runs.
- BOUND (non-room) channel: 1:1 delegation through the wrap to the stored
  runner handler, on the bound bot's lane — the same verdict a typed message
  in that channel gets.
- UNBOUND channel: plain delegation, identical to today's typed behavior.
- BOTH ``_voice_input_source`` paths preserve the concierge routing verdicts:
  the ``/voice join`` copy is a ``to_dict`` serialization (the room-targets
  stamp is wire-invisible BY DESIGN, and ``clear_identity`` drops the stored
  binding profile), so the ingress must restate the verdicts from the LIVE
  registry; the programmatic path gets them from ``build_source`` directly.
- FAIL-CLOSED: an unreadable registry degrades to plain delegation — a routing
  failure never breaks voice intake.
"""

from __future__ import annotations

import asyncio
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

from tests.gateway.conftest import _ensure_discord_mock

_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter

_GUILD, _SPEAKER, _CHANNEL = 1, 42, 700
_MEMBER_A = "bot-reminders-a1b2"
_MEMBER_B = "bot-researcher-b2c3"


@pytest.fixture
def voice_rig(tmp_path, monkeypatch):
    """Multiplexed hub gateway with concierge on, plus the voice attributes
    ``_handle_voice_channel_input`` reads (mirroring the routing rig's runner
    construction; the adapter is the REAL DiscordAdapter, so ``build_source``,
    the wrap and the fan-out machinery are all live)."""
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
    ``_concierge_inbound_wrap``. Everything ABOVE it (``handle_message``
    ingress: identity canonicalization, burst gate, guard install, task
    spawn) runs REAL, as does the wrap's fan-out machinery and every member's
    dispatch back through the real base ``handle_message``; the typing and
    delivery plumbing BELOW the handler is what the routing rig also fakes
    and nothing here depends on."""
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
    """One transcribed utterance through the REAL voice ingress. ``join_source``
    stores the ``/voice join`` message's source SERIALIZED first — exactly what
    ``_handle_voice_channel_join`` stores (``to_dict``, the wire format, which
    by design drops the process-local concierge stamps) — so the copy path runs;
    without it ``_voice_input_source`` takes the programmatic ``build_source``
    path of a programmatic voice join."""
    if join_source:
        join = rig.adapter.build_source(
            chat_id=str(_CHANNEL), chat_type="group", user_id=str(_SPEAKER),
            user_name="Operator", guild_id=str(_GUILD))
        rig.adapter._voice_sources[_GUILD] = join.to_dict()
    await rig.runner._handle_voice_channel_input(
        _GUILD, _SPEAKER, "brief me on the day", adapter=rig.adapter)


async def _drain(adapter, *, timeout: float = 5.0) -> None:
    """Run every task the ingress spawned (the trigger turn, the fan-out
    driver, the member turns) to completion — bounded, so a wedged path fails
    the test instead of hanging it."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        pending = [t for t in list(adapter._background_tasks)
                   if not t.done() and t is not asyncio.current_task()]
        if not pending:
            return
        await asyncio.gather(*pending)
    raise AssertionError("voice ingress tasks did not settle within "
                         f"{timeout}s")


# ── (a) + (d, copy path): a ROOM channel fans out, never the hub lane ──────


@pytest.mark.asyncio
async def test_spoken_room_input_fans_out_to_members(voice_rig, monkeypatch):
    """THE voice-ingress contract: spoken input on a ROOM channel, through the
    ``/voice join`` copy path, carries the room-targets stamp into the wrap —
    the fan-out driver runs EVERY member's turn in stored order, and the hub
    lane never runs (the trigger event itself is consumed by the fan-out)."""
    registry.create_room(str(_CHANNEL), [_MEMBER_A, _MEMBER_B])
    records = await _install_seam_recorder(monkeypatch, voice_rig.adapter)

    await _speak(voice_rig, join_source=True)
    await _drain(voice_rig.adapter)

    # exactly one delegated turn per member, in the registry's stored order;
    # a hub-lane turn for the trigger (profile None) would appear here too —
    # its absence is the fan-out verdict.
    assert records == [
        (_MEMBER_A, MessageType.VOICE, _GUILD),
        (_MEMBER_B, MessageType.VOICE, _GUILD),
    ]


# ── (d, programmatic path): a room join without a stored source ────────────


@pytest.mark.asyncio
async def test_spoken_room_input_programmatic_join_fans_out(voice_rig, monkeypatch):
    """The OTHER ``_voice_input_source`` path: a programmatic voice join binds
    no stored copy, so the source is built by the adapter's ``build_source``
    (which stamps the room targets directly); the fan-out still runs and the
    spoken provenance (VOICE type, raw guild) survives the member clones."""
    registry.create_room(str(_CHANNEL), [_MEMBER_A, _MEMBER_B])
    records = await _install_seam_recorder(monkeypatch, voice_rig.adapter)

    await _speak(voice_rig, join_source=False)
    await _drain(voice_rig.adapter)

    assert records == [
        (_MEMBER_A, MessageType.VOICE, _GUILD),
        (_MEMBER_B, MessageType.VOICE, _GUILD),
    ]


# ── (b): a BOUND non-room channel delegates 1:1 to the bot's lane ───────────


@pytest.mark.asyncio
async def test_spoken_bound_channel_delegates_to_the_bot_lane(voice_rig, monkeypatch):
    """No room stamp on a BOUND channel → the wrap delegates 1:1 to the stored
    runner handler, on the bound bot's profile — the same lane a TYPED message
    in that channel takes (the serialized copy carried the binding, but
    ``clear_identity`` drops it, so the ingress must restate it from the live
    registry). One delegation, no fan-out."""
    registry.bind_channel(
        str(_CHANNEL), profile=_MEMBER_A, blueprint="reminders",
        guild_id=str(_GUILD))
    records = await _install_seam_recorder(monkeypatch, voice_rig.adapter)

    await _speak(voice_rig, join_source=True)
    await _drain(voice_rig.adapter)

    assert records == [(_MEMBER_A, MessageType.VOICE, _GUILD)]


# ── (b, programmatic path): BOUND channel, build_source source ──────────────


@pytest.mark.asyncio
async def test_spoken_bound_channel_programmatic_join_delegates_to_the_bot_lane(
        voice_rig, monkeypatch):
    """The OTHER bound-channel restamp path: a programmatic voice join binds
    no stored copy, so ``_voice_input_source`` builds the source via the
    adapter's ``build_source`` — which stamps the binding profile, only for
    ``clear_identity`` to drop it right after (it clears the previous
    speaker's routing result). The restamp hook must restore the profile
    from the live registry before the wrap delegates, or the utterance
    lands on the hub lane instead of the bound bot's. One delegation, no
    fan-out, spoken provenance preserved."""
    registry.bind_channel(
        str(_CHANNEL), profile=_MEMBER_A, blueprint="reminders",
        guild_id=str(_GUILD))
    records = await _install_seam_recorder(monkeypatch, voice_rig.adapter)

    await _speak(voice_rig, join_source=False)
    await _drain(voice_rig.adapter)

    assert records == [(_MEMBER_A, MessageType.VOICE, _GUILD)]


# ── (c): an UNBOUND channel is plain delegation, typed behavior unchanged ──


@pytest.mark.asyncio
async def test_spoken_unbound_channel_delegates_plain(voice_rig, monkeypatch):
    """Unbound channel → the wrap is a pure pass-through: one delegation, no
    profile stamp, no fan-out — identical to today's typed behavior."""
    records = await _install_seam_recorder(monkeypatch, voice_rig.adapter)

    await _speak(voice_rig, join_source=True)
    await _drain(voice_rig.adapter)

    assert records == [(None, MessageType.VOICE, _GUILD)]


# ── FAIL-CLOSED: a routing failure degrades to normal delegation ───────────


@pytest.mark.asyncio
async def test_unreadable_registry_degrades_to_plain_delegation(
        voice_rig, monkeypatch):
    """Fail-closed intake: with the registry unreadable the utterance loses
    its routing verdicts entirely and takes the normal delegation path — one
    hub-lane turn, no fan-out, no exception."""
    from plugins.platforms.discord.concierge_mixin import ConciergeDiscordMixin

    registry.create_room(str(_CHANNEL), [_MEMBER_A, _MEMBER_B])
    reg_file = voice_rig.root / "concierge" / "registry.json"
    reg_file.write_text("sentinel")
    reg_file.chmod(0o000)
    ConciergeDiscordMixin._concierge_warned.clear()
    try:
        records = await _install_seam_recorder(monkeypatch, voice_rig.adapter)
        await _speak(voice_rig, join_source=True)
        await _drain(voice_rig.adapter)
        assert records == [(None, MessageType.VOICE, _GUILD)]
    finally:
        ConciergeDiscordMixin._concierge_warned.clear()
        reg_file.chmod(0o644)


# ── Stale-stamp hygiene: a fresh verdict outranks an earlier one ────────────


def test_fresh_not_room_verdict_clears_a_stale_room_stamp(voice_rig):
    """The verdict tail must CLEAR a room stamp an earlier verdict left on the
    source: the stamp landed while the channel was a room (``build_source``),
    the room was disbanded before the restamp hook ran, and the fresh
    not-a-room verdict must not leave the stale stamp behind — it would
    outrank the fresh verdict and keep fanning the utterance out to
    disbanded members. Clearing is safe: the wrap's consumer treats an
    absent stamp exactly like a never-set one."""
    from plugins.platforms.discord import concierge_mixin

    registry.create_room(str(_CHANNEL), [_MEMBER_A, _MEMBER_B])
    source = voice_rig.adapter.build_source(
        chat_id=str(_CHANNEL), chat_type="group", user_id=str(_SPEAKER),
        user_name="Operator", guild_id=str(_GUILD))
    assert getattr(source, concierge_mixin._ROOM_TARGETS_ATTR, None)  # the stale stamp

    for member in registry.room_members(str(_CHANNEL)):
        registry.remove_from_room(str(_CHANNEL), member)  # last removal deletes the room

    voice_rig.adapter._restate_voice_routing_stamps(source)

    assert getattr(source, concierge_mixin._ROOM_TARGETS_ATTR, None) is None
