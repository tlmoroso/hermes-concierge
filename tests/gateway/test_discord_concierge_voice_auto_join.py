"""Concierge voice auto-join (``concierge.voice_auto_join``).

An allowlisted user joining a Discord voice channel makes the bot auto-join
that channel with the transcription binding pointed at THE JOINER'S
ORCHESTRATOR HOME CHANNEL — so their spoken input routes to their
orchestrator through the EXISTING voice machinery (restamp → orchestrator
lane → relay summary playback) with zero new routing logic. Default OFF:
zero behavior change.

Contracts, asserted at the real seams (the adapter's ``join_voice_channel``
seam driven with the right args, and ``_voice_text_channels`` state):

- Flag ON + allowlisted member with a live orchestrator + home channel →
  the join seam fires ONCE with ``text_channel_id`` == the home channel.
- Every gate failure (flag off, no/dead orchestrator, no home channel,
  unallowlisted member, bot member, bot already holding the guild's voice
  client, a leave event) → silent no-op, manual ``/voice join`` unchanged.
- Join failure (False/raises) → ONE warning per (guild, user), never a
  propagation, bookkeeping unaffected.
- The adapter's ``on_voice_state_update`` tail calls the duck-typed hook
  AFTER the existing bookkeeping, and a hook failure never breaks it.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from concierge import registry
from gateway.config import GatewayConfig, Platform, PlatformConfig

from tests.gateway.conftest import _ensure_discord_mock

_ensure_discord_mock()

import plugins.platforms.discord.adapter as discord_platform
from gateway.platforms.base import BasePlatformAdapter
from plugins.platforms.discord.adapter import DiscordAdapter

_GUILD, _SPEAKER = 1, 42
_VC, _OTHER_VC, _HOME_CHANNEL = 900, 901, 700
_ORCHESTRATOR = "bot-orchestrator-9f8e"
_GHOST = "bot-orchestrator-dead00"


@pytest.fixture
def autojoin_rig(tmp_path, monkeypatch):
    """Multiplexed hub gateway with concierge on (flag OFF by default — the
    DEFAULT_CONFIG merge posture), plus the voice attrs the hook reads
    (mirroring the voice-ingress rig: the adapter is the REAL DiscordAdapter,
    so ``_concierge_settings_obj`` / ``_concierge_events_wanted`` and the
    registry helpers are all live)."""
    root = tmp_path
    (root / "profiles" / _ORCHESTRATOR).mkdir(parents=True)
    (root / "profiles" / _ORCHESTRATOR / ".env").write_text("OPENAI_API_KEY=sk-x\n")
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
    adapter._client = None  # no discord client: guild/channel lookups degrade
    adapter._voice_clients = {}
    adapter._voice_text_channels = {}
    adapter._voice_sources = {}
    adapter._allowed_user_ids = {str(_SPEAKER)}
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {}
    # Warn-once keys are CLASS-level (one incident per registry state); a
    # previous test's warning must not silence this one's assertion.
    from plugins.platforms.discord.concierge_mixin import ConciergeDiscordMixin

    ConciergeDiscordMixin._concierge_warned.clear()
    return SimpleNamespace(root=root, runner=runner, adapter=adapter)


def _flag_on(rig) -> None:
    rig.runner.config.concierge["voice_auto_join"] = True


def _register_joiner(*, orchestrator=_ORCHESTRATOR, home_channel=str(_HOME_CHANNEL)) -> None:
    registry.set_user(str(_SPEAKER), orchestrator=orchestrator,
                      home_channel=home_channel, guild_id=str(_GUILD))


def _voice_event(*, member_id=_SPEAKER, member_bot=False, joined=True,
                 other_channel=False):
    """One (member, before, after) voice-state triple: a JOIN by default, a
    LEAVE with ``joined=False``, and a channel SWITCH with
    ``other_channel=True`` (before/after both non-None, different ids)."""
    guild = SimpleNamespace(id=_GUILD)
    channel = SimpleNamespace(
        id=_OTHER_VC if other_channel else _VC,
        name="General VC", guild=guild)
    member = SimpleNamespace(id=member_id, bot=member_bot,
                             display_name="Operator", guild=guild)
    if joined:
        # a SWITCH: before is a DIFFERENT channel object (discord channel
        # equality is by identity in the rig, by id in production)
        before_channel = (SimpleNamespace(id=_VC, name="Old VC", guild=guild)
                           if other_channel else None)
        before = SimpleNamespace(channel=before_channel)
        after = SimpleNamespace(channel=channel)
    else:
        before = SimpleNamespace(channel=channel)
        after = SimpleNamespace(channel=None)
    return member, before, after


def _install_join_recorder(monkeypatch, adapter, *, fail=False, raise_exc=None):
    """Replace ``join_voice_channel`` on the ADAPTER THE HOOK RUNS ON with a
    recorder that mimics the seam's binding side effect
    (``_voice_text_channels``) on success — the same state the REAL seam
    writes, so the assert proves the binding, not just the call."""
    calls = []

    async def _join(channel, *, text_channel_id=None, source=None):
        calls.append((getattr(channel, "id", None), text_channel_id, source))
        if raise_exc is not None:
            raise raise_exc
        if fail:
            return False
        adapter._voice_text_channels[_GUILD] = text_channel_id
        return True

    monkeypatch.setattr(adapter, "join_voice_channel", _join)
    return calls


async def _drain(adapter, *, timeout: float = 5.0) -> None:
    """Run every task the hook spawned (the join runs as a background task)
    to completion — bounded, so a wedged path fails the test."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        pending = [t for t in list(getattr(adapter, "_background_tasks", set()))
                   if not t.done() and t is not asyncio.current_task()]
        if not pending:
            return
        await asyncio.gather(*pending)
    raise AssertionError("voice auto-join tasks did not settle within "
                         f"{timeout}s")


# ── (1) Flag ON: the join seam fires, bound to the joiner's home channel ────


@pytest.mark.asyncio
async def test_flag_on_auto_joins_bound_to_the_home_channel(
        autojoin_rig, monkeypatch):
    """THE contract: an allowlisted member with a live orchestrator joins a
    voice channel → the bot joins that channel with the transcription
    binding == the joiner's home channel (so spoken input routes to their
    orchestrator through the existing machinery)."""
    _flag_on(autojoin_rig)
    _register_joiner()
    calls = _install_join_recorder(monkeypatch, autojoin_rig.adapter)

    member, before, after = _voice_event()
    await autojoin_rig.adapter._concierge_maybe_auto_join_voice(member, before, after)
    await _drain(autojoin_rig.adapter)

    assert calls == [(_VC, _HOME_CHANNEL, None)]
    assert autojoin_rig.adapter._voice_text_channels[_GUILD] == _HOME_CHANNEL


# ── (2) Flag OFF: zero behavior change (the default) ────────────────────────


@pytest.mark.asyncio
async def test_flag_off_never_joins(autojoin_rig, monkeypatch):
    """Default OFF: a fully-eligible joiner gets NO auto-join — manual
    ``/voice join`` stays the only way in."""
    _register_joiner()
    calls = _install_join_recorder(monkeypatch, autojoin_rig.adapter)

    member, before, after = _voice_event()
    await autojoin_rig.adapter._concierge_maybe_auto_join_voice(member, before, after)
    await _drain(autojoin_rig.adapter)

    assert calls == []
    assert _GUILD not in autojoin_rig.adapter._voice_text_channels


# ── (3) No orchestrator / dead profile / no home channel → no join ─────────


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["no-entry", "ghost-profile", "no-home-channel"])
async def test_member_without_a_live_home_never_joins(
        autojoin_rig, monkeypatch, case):
    """The liveness posture (``_profile_exists``, the same primitive the
    orchestrator stamp uses): a member with NO users entry, a DEAD profile
    (retired bot), or a live orchestrator with no home_channel gets NO
    auto-join — and no crash, no registry write."""
    if case == "no-entry":
        member, before, after = _voice_event(member_id=_SPEAKER + 1000)
        autojoin_rig.adapter._allowed_user_ids = {str(_SPEAKER + 1000)}
    elif case == "ghost-profile":
        _register_joiner(orchestrator=_GHOST)  # profile dir was never created
        member, before, after = _voice_event()
    else:
        _register_joiner(home_channel=None)  # live profile, but NO home channel
        member, before, after = _voice_event()
    calls = _install_join_recorder(monkeypatch, autojoin_rig.adapter)
    _flag_on(autojoin_rig)

    await autojoin_rig.adapter._concierge_maybe_auto_join_voice(member, before, after)
    await _drain(autojoin_rig.adapter)

    assert calls == []


# ── (4) Unallowlisted member → no join ──────────────────────────────────────


@pytest.mark.asyncio
async def test_unallowlisted_member_never_joins(autojoin_rig, monkeypatch):
    """The hub .env allowlist still gates the feature (via the adapter's
    ``_allowed_user_ids`` — the SAME set the voice receiver is constructed
    with, never a re-parse of the env): a stranger joining a voice channel
    must not summon the bot."""
    _flag_on(autojoin_rig)
    _register_joiner()
    autojoin_rig.adapter._allowed_user_ids = {str(_SPEAKER + 1000)}
    calls = _install_join_recorder(monkeypatch, autojoin_rig.adapter)

    member, before, after = _voice_event()
    await autojoin_rig.adapter._concierge_maybe_auto_join_voice(member, before, after)
    await _drain(autojoin_rig.adapter)

    assert calls == []


# ── (5) Bot member → no join (anti-loop) ────────────────────────────────────


@pytest.mark.asyncio
async def test_bot_member_never_joins(autojoin_rig, monkeypatch):
    """A BOT's voice state (another bot joining/moving) never triggers a
    join — the anti-loop guard every concierge lane carries."""
    _flag_on(autojoin_rig)
    _register_joiner()
    calls = _install_join_recorder(monkeypatch, autojoin_rig.adapter)

    member, before, after = _voice_event(member_bot=True)
    await autojoin_rig.adapter._concierge_maybe_auto_join_voice(member, before, after)
    await _drain(autojoin_rig.adapter)

    assert calls == []


# ── (6) Bot already holding the guild's voice client → second joiner ignored ─


@pytest.mark.asyncio
async def test_second_joiner_ignored_while_the_bot_holds_a_voice_client(
        autojoin_rig, monkeypatch):
    """One voice client per guild: while the bot is already in a voice
    channel there, another member joining ELSEWHERE is IGNORED — no moves,
    no re-binding (``join_voice_channel``'s move_to path is never reached)."""
    _flag_on(autojoin_rig)
    _register_joiner()
    vc_channel = SimpleNamespace(id=_OTHER_VC, name="Other VC")
    autojoin_rig.adapter._voice_clients[_GUILD] = SimpleNamespace(
        channel=vc_channel, is_connected=lambda: True)
    calls = _install_join_recorder(monkeypatch, autojoin_rig.adapter)

    member, before, after = _voice_event()
    await autojoin_rig.adapter._concierge_maybe_auto_join_voice(member, before, after)
    await _drain(autojoin_rig.adapter)

    assert calls == []
    # the existing binding (whatever a manual /voice join made) is untouched
    assert autojoin_rig.adapter._voice_clients[_GUILD].channel is vc_channel


# ── (7) Join failure: warn once per (guild, user), never propagate ────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["false", "raises"], ids=["returns-false", "raises"])
async def test_join_failure_warns_once_and_never_propagates(
        autojoin_rig, monkeypatch, caplog, mode):
    """A join failure (the seam returns False, or raises) logs ONE warning
    per (guild, user) — a second identical failure stays silent — and never
    propagates: the voice-state bookkeeping is unaffected and a repeat event
    still completes without crashing."""
    _flag_on(autojoin_rig)
    _register_joiner()
    calls = _install_join_recorder(
        monkeypatch, autojoin_rig.adapter,
        raise_exc=RuntimeError("voice connect refused") if mode == "raises" else None,
        fail=(mode == "false"))

    member, before, after = _voice_event()
    with caplog.at_level(logging.WARNING, logger="plugins.platforms.discord.concierge_mixin"):
        await autojoin_rig.adapter._concierge_maybe_auto_join_voice(member, before, after)
        await _drain(autojoin_rig.adapter)
        # a second identical failure: same (guild, user) key → still ONE warning
        await autojoin_rig.adapter._concierge_maybe_auto_join_voice(member, before, after)
        await _drain(autojoin_rig.adapter)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "join failure must warn exactly once per (guild, user)"
    assert "voice" in warnings[0].getMessage().lower()
    assert len(calls) == 2  # both attempts ran, both failed closed
    assert _GUILD not in autojoin_rig.adapter._voice_text_channels  # no binding


# ── (8) Leave events never join ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_leave_event_never_joins(autojoin_rig, monkeypatch):
    """``after.channel is None`` (a leave) is not a join — no join attempt,
    ever (the idle bookkeeping above it is all a leave owes)."""
    _flag_on(autojoin_rig)
    _register_joiner()
    calls = _install_join_recorder(monkeypatch, autojoin_rig.adapter)

    member, before, after = _voice_event(joined=False)
    await autojoin_rig.adapter._concierge_maybe_auto_join_voice(member, before, after)
    await _drain(autojoin_rig.adapter)

    assert calls == []


# ── (8b) A channel SWITCH counts as a join (before != after, both set) ──────


@pytest.mark.asyncio
async def test_channel_switch_joins_the_new_channel(
        autojoin_rig, monkeypatch):
    """The event is a join when ``after.channel`` differs from
    ``before.channel`` — moving between voice channels re-binds to the NEW
    channel (same gate the spec pins: "not None and differs")."""
    _flag_on(autojoin_rig)
    _register_joiner()
    calls = _install_join_recorder(monkeypatch, autojoin_rig.adapter)

    member, before, after = _voice_event(other_channel=True)
    await autojoin_rig.adapter._concierge_maybe_auto_join_voice(member, before, after)
    await _drain(autojoin_rig.adapter)

    assert calls == [(_OTHER_VC, _HOME_CHANNEL, None)]


# ── The adapter tail: the registered event drives the hook, fail-safe ───────


class _FakeTree:
    def __init__(self):
        self.sync = AsyncMock(return_value=[])
        self.fetch_commands = AsyncMock(return_value=[])
        self._commands = []

    def command(self, *args, **kwargs):
        return lambda fn: fn

    def get_commands(self, *args, **kwargs):
        return list(self._commands)


class _FakeBot:
    """Minimal stand-in for ``discord.ext.commands.Bot``: ``event`` records
    the closures ``connect`` registers, ``start`` fires ``on_ready``."""

    def __init__(self, *, intents, proxy=None, allowed_mentions=None, **_):
        self.intents = intents
        self.allowed_mentions = allowed_mentions
        self.application_id = 999
        self.user = SimpleNamespace(id=999, name="Hermes")
        self._events = {}
        self.tree = _FakeTree()
        self.http = SimpleNamespace(
            upsert_global_command=AsyncMock(),
            edit_global_command=AsyncMock(),
            delete_global_command=AsyncMock(),
        )

    def event(self, fn):
        self._events[fn.__name__] = fn
        return fn

    async def start(self, token):
        if "on_ready" in self._events:
            await self._events["on_ready"]()

    async def close(self):
        return None


@pytest.fixture
def connected_rig(autojoin_rig, monkeypatch):
    """A REAL ``DiscordAdapter.connect()`` (FakeBot rig, the
    test_discord_connect pattern) so the REGISTERED
    ``on_voice_state_update`` closure — the production tail seam — is
    reachable, driving the real adapter state the hook reads."""
    monkeypatch.setattr(
        DiscordAdapter, "_command_sync_mutation_interval_seconds",
        lambda self: 0.0)
    monkeypatch.setattr(
        "gateway.status.acquire_scoped_lock",
        lambda scope, identity, metadata=None: (True, None))
    monkeypatch.setattr(
        "gateway.status_scoped_locks.release_scoped_lock",
        lambda scope, identity: None)
    intents = SimpleNamespace(
        message_content=False, dm_messages=False, guild_messages=False,
        members=False, voice_states=False)
    monkeypatch.setattr(discord_platform.Intents, "default", lambda: intents)

    created: list = []

    def _factory(*, command_prefix, intents, proxy=None, allowed_mentions=None, **_):
        bot = _FakeBot(intents=intents, allowed_mentions=allowed_mentions)
        created.append(bot)
        return bot

    monkeypatch.setattr(discord_platform.commands, "Bot", _factory)

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    monkeypatch.setattr(adapter, "_resolve_allowed_usernames", AsyncMock())

    async def _connect():
        assert await adapter.connect() is True
        adapter.gateway_runner = autojoin_rig.runner
        adapter._allowed_user_ids = {str(_SPEAKER)}
        return adapter

    return SimpleNamespace(connect=_connect, bots=created,
                            base_rig=autojoin_rig)


@pytest.mark.asyncio
async def test_registered_event_drives_the_hook_end_to_end(
        connected_rig, monkeypatch):
    """The production seam: the closure ``connect()`` registered calls the
    duck-typed hook AFTER its bookkeeping, and with the flag ON the REAL
    registered ``on_voice_state_update`` drives a full auto-join — voice
    channel joined with the transcription binding == the home channel."""
    rig = connected_rig.base_rig
    _flag_on(rig)
    _register_joiner()
    adapter = await connected_rig.connect()
    calls = _install_join_recorder(monkeypatch, adapter)

    member, before, after = _voice_event()
    await connected_rig.bots[-1]._events["on_voice_state_update"](member, before, after)
    await _drain(adapter)

    assert calls == [(_VC, _HOME_CHANNEL, None)]


@pytest.mark.asyncio
async def test_hook_failure_never_breaks_the_voice_state_bookkeeping(
        connected_rig, monkeypatch):
    """Fail-safe tail: a hook that RAISES must not break the existing
    bookkeeping — the closure swallows it, the DAVE epoch refresh (the one
    side effect the bookkeeping owns) still runs, and no exception escapes
    ``on_voice_state_update``."""
    rig = connected_rig.base_rig
    _flag_on(rig)
    _register_joiner()
    adapter = await connected_rig.connect()

    async def _boom(member, before, after):
        raise RuntimeError("hook blew up")

    monkeypatch.setattr(adapter, "_concierge_maybe_auto_join_voice", _boom)

    member, before, after = _voice_event()
    receiver = MagicMock()
    adapter._voice_clients[_GUILD] = SimpleNamespace(
        channel=after.channel, is_connected=lambda: True)
    adapter._voice_receivers[_GUILD] = receiver

    handler = connected_rig.bots[-1]._events["on_voice_state_update"]

    await handler(member, before, after)  # must NOT raise
    receiver.refresh_credentials.assert_called_once_with("membership change")


# ── Settings reader contract (DEFAULT_CONFIG key ↔ ConciergeSettings reader) ─


def test_default_config_has_voice_auto_join_key():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["concierge"]["voice_auto_join"] is False


def test_reader_fails_closed_to_off_and_honors_explicit_true(
        autojoin_rig, monkeypatch):
    """The typed reader: absent/junk values resolve OFF (the zero-change
    default); an explicit true/``on`` turns it on; YAML bools honored."""
    from concierge.settings import concierge_settings
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert concierge_settings().voice_auto_join is False  # defaults only
    settings = concierge_settings({"concierge": {"voice_auto_join": True}})
    assert settings.voice_auto_join is True
    for garbage in (None, "not-a-bool", 5, [], {}):
        settings = concierge_settings({"concierge": {"voice_auto_join": garbage}})
        assert settings.voice_auto_join is False, garbage
    settings = concierge_settings({"concierge": {"voice_auto_join": "on"}})
    assert settings.voice_auto_join is True
    # DEFAULT_CONFIG merge posture: an explicit partial section keeps the flag
    # OFF unless the key itself is set (the launch-home override rule).
    settings = concierge_settings({"concierge": {"enabled": True}})
    assert settings.voice_auto_join is DEFAULT_CONFIG["concierge"]["voice_auto_join"]