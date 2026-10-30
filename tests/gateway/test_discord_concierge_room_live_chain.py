"""The room fan-out + connect nudges must ride the RUNNER's handler seam.

Live bug (2026-10-29, reproduced): a Discord message in a registered ROOM
channel was handled by the HUB lane — the fan-out to the member bots never
ran, zero member-turn log lines. Upstream's inbound refactor moved the live
chain to ``BaseAdapter._start_session_processing`` →
``_process_message_background`` → ``self._message_handler(event)`` — the
runner handler installed via ``set_message_handler`` — and the mixin's
``handle_message`` override (where the room fan-out and the Task 8.2 connect
nudge lived) fell out of that chain: dead code on the live path.

The fix keeps the fan-out in exactly ONE place: ``set_message_handler`` is
overridden to install a CONCIERGE WRAP as ``_message_handler`` (the runner's
own seam — every live dispatch funnels through it, whatever entry computed
the session key). The wrap consumes the room-targets stamp (fan-out driver,
never delegated), posts the connect nudge before delegating, and otherwise
delegates 1:1 to the stored runner handler — fail-closed on any concierge
branch error. Member turns still ride the base's full dispatch lane
(``super().handle_message`` → ``_start_session_processing`` →
``_process_message_background`` → the wrap → the stored runner handler), so
admission, scope, busy/queue handling AND the adapter-side response delivery
all run exactly as for a single-bot message.

Same file, same task: hub-created channels suppress the create-side
auto-offer (a fresh ``#room-…`` used to get a bot picker posted into it
before the room registered) via an in-memory TTL set populated at the ONE
channel-creation site both flows share.

Rig: the routing suite's multiplex-hub shape — a real ``DiscordAdapter`` over
the conftest discord mock, a real temp registry under an isolated
HERMES_HOME, and a SPY as the runner handler set through
``set_message_handler`` (the live wire path).
"""

import asyncio
import socket
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from concierge import registry
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.profile_routing import parse_profile_routes
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent

from tests.gateway.conftest import _ensure_discord_mock

_ensure_discord_mock()

from plugins.platforms.discord import concierge_mixin
from plugins.platforms.discord.adapter import DiscordAdapter


def _run(coro):
    """Run one scenario coroutine on a fresh loop."""
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Multiplexed hub adapter with concierge on and live bot profile dirs
    (the routing suite's rig shape: a REAL adapter over the discord mock, a
    REAL temp registry under an isolated HERMES_HOME)."""
    root = tmp_path
    for name in ("bot-reminders-a1b2", "bot-researcher-b2c3"):
        (root / "profiles" / name).mkdir(parents=True)
        (root / "profiles" / name / ".env").write_text("K=1\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    (root / "concierge").mkdir()

    # Live-install location artifact: gateway/run.py imports hermes_bootstrap,
    # whose MODULE-LEVEL PM activation stats the REAL ~/.hermes/manifest.json
    # when the checkout lives inside the home — tripping tests/home_io_guard
    # at fixture setup. Stub the module BEFORE the import (same shape
    # gateway.run.py already tolerates for an unregistered bootstrap).
    _bootstrap = types.ModuleType("hermes_bootstrap")
    _bootstrap._happy_eyeballs_create_connection = socket.create_connection
    sys.modules.setdefault("hermes_bootstrap", _bootstrap)

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.platforms = {Platform.DISCORD: PlatformConfig(enabled=True, extra={})}
    runner.config.profile_routes = parse_profile_routes([])
    runner.pairing_store = SimpleNamespace()
    runner.pairing_stores = {}
    runner._primary_profile_name = "default"

    adapter = object.__new__(DiscordAdapter)
    BasePlatformAdapter.__init__(
        adapter, PlatformConfig(enabled=True, extra={}), Platform.DISCORD)
    adapter.gateway_runner = runner
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {}

    runner.config.concierge = {"enabled": True}
    return SimpleNamespace(root=root, runner=runner, adapter=adapter)


def _room_trigger(adapter, chat_id="100"):
    """A MessageEvent whose source carries the REAL build_source room stamp —
    exactly what the live inbound chain handed the dead override."""
    source = adapter.build_source(chat_id=str(chat_id), chat_type="group", user_id="u1")
    assert getattr(source, concierge_mixin._ROOM_TARGETS_ATTR), "room stamp missing"
    return MessageEvent(text="hello room", source=source, raw_message=None)


def _plain_event(adapter, chat_id="555", text="a plain message"):
    source = adapter.build_source(chat_id=str(chat_id), chat_type="group", user_id="u1")
    return MessageEvent(text=text, source=source, raw_message=None)


def _spy(calls, *, result=None):
    """A runner-handler spy: records each event it is handed."""
    async def _handler(event):
        calls.append(event)
        return result

    return _handler


# ── (a) THE LIVE-BUG DOC TEST: the runner seam consumes the room stamp ─────


def test_room_message_fans_out_through_the_runner_handler_seam(rig, monkeypatch):
    """THE fixed contract, at the seam the live chain actually calls: with the
    runner handler installed via ``set_message_handler`` and a room-stamped
    event driven through ``_message_handler`` (the exact tail
    ``_process_message_background`` awaits), the fan-out driver is spawned
    and tracked, the ORIGINAL event never reaches the runner handler, and the
    member turns arrive as SEPARATE events — one per member, in the
    registry's stored order, each stamped with that member's profile."""
    registry.create_room("100", ["bot-reminders-a1b2", "bot-researcher-b2c3"])
    calls = []
    rig.adapter.set_message_handler(_spy(calls))
    # The typing refresh is platform plumbing, not the seam under test.
    monkeypatch.setattr(rig.adapter, "_start_typing_refresh", lambda *a, **k: None)

    trigger = _room_trigger(rig.adapter)

    async def _scenario():
        # The live lane's tail: _process_message_background awaits exactly
        # this attribute — the wrap set_message_handler installed.
        await rig.adapter._message_handler(trigger)
        task = rig.adapter._concierge_room_fanout_tasks["100"]
        assert task is not None and not task.done()
        assert calls == []  # the trigger was CONSUMED, never dispatched
        await task  # the driver runs the members sequentially
        for t in list(rig.adapter._session_tasks.values()):
            if not t.done():
                await t

    _run(_scenario())

    # one dispatch per member, in stored order, each a fresh event stamped
    # with that member's profile — and the trigger is none of them
    assert len(calls) == 2
    assert [getattr(ev.source, "profile", None) for ev in calls] == [
        "bot-reminders-a1b2", "bot-researcher-b2c3"]
    assert all(ev is not trigger for ev in calls)
    assert calls[0] is not calls[1]
    # each member turn rode the member's OWN profile-namespaced session lane
    for ev in calls:
        assert rig.adapter._event_session_key(ev).startswith(
            f"agent:{ev.source.profile}:")


# ── (b) wrap idempotency: never double-wrap, never lose a rewire ───────────


def test_setting_the_handler_twice_never_double_wraps(rig):
    calls1, calls2 = [], []
    rig.adapter.set_message_handler(_spy(calls1))
    wrap = rig.adapter._message_handler
    assert wrap is not None and not callable(getattr(wrap, "__wrapped__", None))

    rig.adapter.set_message_handler(_spy(calls2))

    # the SAME single wrap stays installed; only the stored runner handler
    # was swapped underneath it
    assert rig.adapter._message_handler is wrap
    assert rig.adapter._concierge_runner_message_handler is not None

    _run(rig.adapter._message_handler(_plain_event(rig.adapter)))
    assert calls2 and not calls1  # the LATEST handler answers, exactly once


def test_clearing_then_resetting_the_handler_rewraps_cleanly(rig):
    """``set_message_handler(None)`` (the shutdown/clear call) restores the
    base's exact clear semantics; the next non-None set installs a fresh
    wrap around the NEW handler — the stored stale one is never invoked."""
    calls = []
    rig.adapter.set_message_handler(_spy(calls))
    rig.adapter.set_message_handler(None)
    assert rig.adapter._message_handler is None
    assert rig.adapter._concierge_runner_message_handler is None
    assert calls == []


def test_resetting_after_clear_installs_a_working_wrap(rig):
    stale, fresh = [], []
    rig.adapter.set_message_handler(_spy(stale))
    rig.adapter.set_message_handler(None)
    rig.adapter.set_message_handler(_spy(fresh))

    event = _plain_event(rig.adapter)
    _run(rig.adapter._message_handler(event))
    assert fresh == [event] and stale == []


# ── (c) fail-closed: a concierge-branch error never breaks intake ──────────


def test_stamp_read_failure_delegates_to_the_runner_handler(rig, monkeypatch):
    """FAIL-CLOSED: if reading the room-targets stamp itself raises, the
    message is still delivered — delegated to the stored runner handler."""
    calls = []
    rig.adapter.set_message_handler(_spy(calls, result="ok"))
    registry.create_room("100", ["bot-reminders-a1b2"])

    def _boom(event):
        raise RuntimeError("stamp read exploded")

    monkeypatch.setattr(rig.adapter, "_concierge_room_targets_on", _boom)

    async def _scenario():
        trigger = _room_trigger(rig.adapter)
        result = await rig.adapter._message_handler(trigger)
        return trigger, result

    trigger, result = _run(_scenario())

    assert result == "ok"        # delegated, return propagated
    assert calls == [trigger]     # exactly once, with the original event


def test_fanout_spawn_failure_delegates_and_consumes_the_trigger(rig, monkeypatch):
    """FAIL-CLOSED on the spawn side: if the fan-out cannot be spawned (here:
    the task-tracking step raises), the wrapper falls back to the normal lane
    — and the half-spawned driver is CANCELLED so the message can never run
    twice (once as a member fan-out, once as the delegated hub turn)."""
    registry.create_room("100", ["bot-reminders-a1b2", "bot-researcher-b2c3"])
    calls = []
    rig.adapter.set_message_handler(_spy(calls, result="ok"))

    def _boom(channel_id, task):
        raise RuntimeError("tracking exploded")

    monkeypatch.setattr(rig.adapter, "_concierge_track_fanout_task", _boom)

    async def _scenario():
        trigger = _room_trigger(rig.adapter)
        result = await rig.adapter._message_handler(trigger)
        # let the loop deliver the orphan's cancellation and prove no member
        # turn ever dispatched through it
        for _ in range(10):
            await asyncio.sleep(0)
        return trigger, result

    trigger, result = _run(_scenario())

    assert result == "ok"
    # delegated exactly once with the ORIGINAL event — and NO member events
    # ever arrived (the orphan driver was cancelled before it ran a member)
    assert calls == [trigger]
    assert getattr(rig.adapter, "_concierge_room_fanout_tasks", {}) == {}


# ── (d) non-stamped events: a 1:1 pass-through ─────────────────────────────


def test_unstamped_event_delegates_one_to_one(rig):
    """The common case is byte-identical to the base behavior: an event with
    no concierge stamps is delegated exactly once, as the SAME object, and
    the runner handler's return value propagates untouched."""
    calls = []
    rig.adapter.set_message_handler(_spy(calls, result="sentinel"))
    event = _plain_event(rig.adapter)

    result = _run(rig.adapter._message_handler(event))

    assert calls == [event]
    assert result == "sentinel"


# ── (e) hub-created channels: no create-side auto-offer ────────────────────


class _FakeChannel:
    def __init__(self, *, channel_id, category_id, sends):
        self.id = int(channel_id)
        self.category_id = int(category_id) if category_id else None
        self._sends = sends

    async def send(self, text, *, view=None):
        self._sends.append((str(self.id), text, view))


def _managed_config(rig):
    rig.runner.config.concierge = {
        "enabled": True, "managed_category_ids": ["555"],
        "default_provider_group": None, "provider_groups": {},
        "hub_profile": "concierge"}


def test_hub_created_channel_gets_no_auto_offer(rig):
    """THE wart fix: a channel id the HUB itself just created (the room-create
    and /bot-new create flows) gets NO bot picker — the creating flow posts
    its own greeting/confirmation into it."""
    sends = []
    channel = _FakeChannel(channel_id="700", category_id="555", sends=sends)
    _managed_config(rig)
    rig.adapter._concierge_remember_hub_created_channel("700")

    _run(rig.adapter._concierge_on_guild_channel_create(channel))

    assert sends == []


def test_a_channel_the_hub_did_not_create_still_gets_the_offer(rig):
    """The suppression is scoped to hub-created ids ONLY: a user-created
    channel in a managed category still gets the picker."""
    sends = []
    channel = _FakeChannel(channel_id="701", category_id="555", sends=sends)
    _managed_config(rig)
    rig.adapter._concierge_remember_hub_created_channel("700")

    _run(rig.adapter._concierge_on_guild_channel_create(channel))

    assert len(sends) == 1
    assert sends[0][0] == "701"


def test_hub_created_entry_expires_after_the_ttl(rig, monkeypatch):
    """A stale TTL entry is harmless: past its deadline the id no longer
    suppresses (a channel id is never reused, but a wedged entry must not
    silence the offer for a genuinely fresh channel forever)."""
    sends = []
    channel = _FakeChannel(channel_id="700", category_id="555", sends=sends)
    _managed_config(rig)
    monkeypatch.setattr(
        concierge_mixin, "_HUB_CREATED_CHANNEL_TTL_SECONDS", -1.0)
    rig.adapter._concierge_remember_hub_created_channel("700")

    _run(rig.adapter._concierge_on_guild_channel_create(channel))

    assert len(sends) == 1  # expired → the offer fired again


def test_the_channel_create_flow_populates_the_ttl_set(rig):
    """The populate site is the ONE channel-creation helper both flows share
    (``/room create`` and ``/bot new``'s create plan): a successful hub-side
    create remembers the new channel id, so the create event's own auto-offer
    is suppressed without either flow knowing about the other."""
    guild = SimpleNamespace(
        create_text_channel=AsyncMock(return_value=SimpleNamespace(id=700)))
    rig.adapter._client = SimpleNamespace(get_guild=lambda _gid: guild,
                                          guilds=[guild])

    created = _run(rig.adapter._concierge_create_channel(
        guild_id="42", category_id="555", name="#room-standup"))

    assert created == "700"
    assert rig.adapter._concierge_channel_is_hub_created("700")
