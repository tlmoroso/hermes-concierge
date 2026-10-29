"""Concierge forum-post flow: in-thread greeting + the opening-content seed.

Live bug (2026-09-27 forum E2E): a bot provisioned into a FORUM POST (a
thread bound at picker selection) never said anything in the thread, and
the post's own opening text (written at post creation, BEFORE the binding
existed) was never routed to the bot — so the new bot sat silent until the
user typed a second message.

Root cause of the missing in-thread confirmation: the provision path only
posts the in-channel greeting when the plan CREATED a channel
(``created_channel_id``), but the forum-post flow takes the BIND plan (the
thread the picker was posted in IS the destination), so the greeting
branch never ran.

Contracts pinned here:
- ``on_guild_channel_create`` skips the picker for FORUM channels (they
  reject plain messages; the thread-create flow owns forum offers) while
  still offering in a managed-category TEXT channel;
- a picker selection that binds a THREAD posts the confirmation greeting
  INTO the thread (the same ``_concierge_post_to_channel`` path the
  created-channel greeting uses) — never only the interaction followup;
- the greeting is followed by ONE seed turn dispatched through the normal
  ``handle_message`` machinery with the post's opening message, stamped to
  the newly bound profile (no second turn mechanism — the fan-out's member
  turn path is reused);
- a PRE-BINDING plain message in the same thread produces neither a
  greeting nor a seed dispatch (the greeting + seed ride the provision
  step only).

Real imports against a temp HERMES_HOME; discord.py objects are stubs
(``tests/gateway/conftest.py`` shapes the module), same rig family as
tests/gateway/test_discord_concierge_commands.py.
"""

import asyncio
import socket
import sys
import types
from types import SimpleNamespace

import pytest

from concierge import registry
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter

from tests.gateway.conftest import _ensure_discord_mock  # noqa: E402

_ensure_discord_mock()

import plugins.platforms.discord.adapter as discord_platform  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


def _run(coro):
    """Run one handler coroutine on a fresh loop (no ambient event loop)."""
    return asyncio.new_event_loop().run_until_complete(coro)


MANAGED_CATEGORY = "555"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Concierge-enabled hub adapter on a multiplexed runner (commands rig)."""
    # The thread fakes must pass production's isinstance verdict: register
    # the fake class as discord.Thread for the whole file (per-test
    # monkeypatch restore keeps the swap isolated).
    monkeypatch.setattr(discord_platform.discord, "Thread", _FakeThread,
                        raising=False)
    root = tmp_path
    for name in ("bot-reminders-a1b2", "bot-researcher-b2c3"):
        (root / "profiles" / name).mkdir(parents=True)
        (root / "profiles" / name / ".env").write_text("K=1\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    (root / "concierge").mkdir()

    # Live-install location artifact (see test_discord_concierge_commands.py).
    _bootstrap = types.ModuleType("hermes_bootstrap")
    _bootstrap._happy_eyeballs_create_connection = socket.create_connection
    sys.modules.setdefault("hermes_bootstrap", _bootstrap)

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.platforms = {Platform.DISCORD: PlatformConfig(enabled=True, extra={})}
    runner.pairing_store = SimpleNamespace()
    runner.pairing_stores = {}
    runner._primary_profile_name = "default"

    adapter = object.__new__(DiscordAdapter)
    BasePlatformAdapter.__init__(
        adapter, PlatformConfig(enabled=True, extra={}), Platform.DISCORD)
    adapter.gateway_runner = runner
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {}

    runner.config.concierge = {
        "enabled": True, "managed_category_ids": [MANAGED_CATEGORY],
        "default_provider_group": None, "provider_groups": {},
        "hub_profile": "concierge"}
    return SimpleNamespace(root=root, runner=runner, adapter=adapter)


# ── stub discord shapes ─────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, interaction=None):
        self.sent = []
        self.deferred = False
        self.defer_ephemeral = []
        self._interaction = interaction

    def is_done(self):
        return bool(self.sent) or self.deferred

    async def send_message(self, text, *, view=None, ephemeral=False):
        self.sent.append((text, view, ephemeral))

    async def defer(self, *, ephemeral=False, **_kw):
        self.deferred = True
        self.defer_ephemeral.append(ephemeral)


class _FakeInteraction:
    def __init__(self, *, channel_id="100", guild_id="42", category_id=None,
                 data=None):
        self.response = _FakeResponse(self)
        self.followup_log = []
        self.followup = SimpleNamespace(send=self._followup_send)
        self.channel = SimpleNamespace(
            id=int(channel_id) if channel_id else None,
            category_id=int(category_id) if category_id else None)
        self.channel_id = int(channel_id) if channel_id else None
        self.guild_id = int(guild_id) if guild_id else None
        self.guild = SimpleNamespace(id=int(guild_id) if guild_id else None)
        self.data = data or {}
        self.user = SimpleNamespace(id="u1", name="U", display_name="U")

    async def _followup_send(self, text, *, view=None, ephemeral=False):
        self.followup_log.append((text, view, ephemeral))


class _FakeThread:
    """A Discord forum post (a thread) with a recorded send log. Registered
    as ``discord.Thread`` per-test (the ``_ensure_thread_class`` fixture, the
    same discipline tests/gateway/test_discord_concierge_free_response.py
    uses) so isinstance-based thread detection — production's verdict —
    answers True whether the conftest mock or real discord.py is loaded."""

    def __init__(self, thread_id=800, parent_id=801, category_id=MANAGED_CATEGORY):
        self.id = int(thread_id)
        self.parent_id = int(parent_id)
        self.parent = SimpleNamespace(
            id=int(parent_id), category_id=int(category_id))
        self.sent = []

    async def send(self, text, *, view=None, **_kw):
        self.sent.append(text)


class _ForumChannelType(int):
    """A discord.ChannelType-forum-shaped enum member (type 15)."""


# ── A) forum channels get no picker ─────────────────────────────────────────


def test_no_picker_for_a_forum_channel(rig):
    """A forum channel (plain messages rejected by Discord) gets NO picker
    from on_guild_channel_create — the thread-create flow owns forum
    offers. Skipped silently: no warning, no failed send attempt."""
    forum = SimpleNamespace(
        id=700, category_id=int(MANAGED_CATEGORY),
        type=_ForumChannelType(15), sends=[])
    # a plain send must never be attempted (Discord would 400 it)
    async def _boom(*a, **kw):
        raise RuntimeError("forum channels reject plain sends")
    forum.send = _boom

    _run(rig.adapter._concierge_on_guild_channel_create(forum))  # no raise

    # nothing was posted and nothing was warned
    assert forum.sends == []


def test_picker_still_offered_for_a_text_channel(rig):
    """The skip must not break the managed-category TEXT channel auto-offer
    (the existing Task 5.5 behavior)."""
    sends = []

    class _TextChannel:
        id = 701
        category_id = int(MANAGED_CATEGORY)

        async def send(self, text, *, view=None):
            sends.append((text, view))

    _run(rig.adapter._concierge_on_guild_channel_create(_TextChannel()))

    assert len(sends) == 1
    assert [o.value for o in sends[0][1].children[0].options] == [
        "reminders", "scheduler", "researcher", "email_triage", "notes"]


# ── B) thread provision: in-thread greeting + seed turn ────────────────────


def _install_turn_recorder(rig, order):
    """Stub the ONE point where the base spawns a turn
    (``_start_session_processing``) with a recorder, the way the room
    fan-out tests do. Everything above it (identity canonicalization,
    profile-namespaced session key, busy-guard install) runs REAL."""

    async def _handler(event):
        return None

    rig.adapter._message_handler = _handler

    def _fake_start(event, session_key, *, interrupt_event=None):
        order.append({
            "kind": "dispatch",
            "text": event.text,
            "profile": getattr(event.source, "profile", None),
            "chat_id": event.source.chat_id,
            "session_key": session_key,
        })

        async def _turn():
            order.append({"kind": "end", "session_key": session_key})

        task = asyncio.create_task(_turn())
        rig.adapter._session_tasks[session_key] = task
        rig.adapter._active_sessions[session_key] = asyncio.Event()
        return True

    rig.adapter._start_session_processing = _fake_start
    return order


def _provision_picker_for(rig, thread):
    """Run the PRODUCTION thread-create auto-offer and return the posted
    picker's view (the posted-in thread IS the destination: bind plan)."""
    _run(rig.adapter._concierge_on_thread_create(thread))
    assert len(thread.sent) == 1            # the picker was offered
    return thread.views[0]


class _ViewThread(_FakeThread):
    """_FakeThread that also records the view attached to each send."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.views = []

    async def send(self, text, *, view=None, **kw):
        self.sent.append(text)
        self.views.append(view)


def test_thread_provision_greets_in_thread_and_seeds_the_post_content(
        rig, monkeypatch):
    """THE forum-post contract: a picker selection that binds a THREAD
    posts the confirmation greeting INTO the thread AND dispatches one
    seed turn carrying the post's opening message, through the normal
    handle_message machinery, stamped to the newly bound profile."""
    thread = _ViewThread(thread_id=800, parent_id=801,
                         category_id=MANAGED_CATEGORY)
    view = _provision_picker_for(rig, thread)

    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)

    def fake_create_bot(blueprint_key, *, channel_id, guild_id=None,
                        provider_group=None, **kw):
        registry.bind_channel(
            str(channel_id), profile="bot-reminders-a1b2",
            blueprint=blueprint_key, guild_id=guild_id)
        return {"profile": "bot-reminders-a1b2", "blueprint": blueprint_key,
                "already_provisioned": False}

    from concierge import provision

    monkeypatch.setattr(provision, "create_bot", fake_create_bot)

    # the post's opening message: available on the thread object (discord.py
    # forum threads carry the starter message), fetched by the seed path
    starter = SimpleNamespace(id=9001, content="Please researchQL the "
                             "hermes repo before Friday")
    thread.starter_message = starter

    order = []
    _install_turn_recorder(rig, order)

    interaction = _FakeInteraction(
        channel_id="800", guild_id="42", category_id=None,
        data={"values": ["reminders"]})
    # Production: the component interaction's channel IS the thread the picker
    # was posted in (discord.py resolves it), so thread detection and the
    # in-thread greeting both target it.
    interaction.channel = thread

    async def _scenario():
        await view.children[0].callback(interaction)
        # let the seed turn task land
        for _key, t in list(rig.adapter._session_tasks.items()):
            if not t.done():
                await t

    asyncio.new_event_loop().run_until_complete(_scenario())

    # 1) the binding landed on the THREAD id
    binding = registry.get_binding("800")
    assert binding is not None
    assert binding["profile"] == "bot-reminders-a1b2"

    # 2) the confirmation greeting landed IN the thread (public post, not
    #    just the interaction followup)
    assert len(thread.sent) == 2
    greeting = thread.sent[1]
    assert "Reminder Bot" in greeting
    assert "say hi" in greeting

    # 3) the seed turn dispatched through the normal machinery, stamped to
    #    the newly bound profile, carrying the post's opening content
    dispatched = [e for e in order if e["kind"] == "dispatch"]
    assert len(dispatched) == 1
    seed = dispatched[0]
    assert seed["profile"] == "bot-reminders-a1b2"
    assert seed["session_key"].startswith("agent:bot-reminders-a1b2:")
    assert "researchQL the" in seed["text"]
    assert "hermes repo" in seed["text"]
    # the seed turn's reply destination is the thread itself
    assert seed["chat_id"] == "800"


def test_pre_binding_thread_message_never_greets_or_seeds(rig, monkeypatch):
    """A plain message that PREDATES the binding (the live bug's shape:
    the post body is written at creation, before any bot exists) dispatches
    normally — it must NOT produce a greeting or a seed turn, because the
    greeting + seed ride the PROVISION step, not message intake."""
    from gateway.platforms.event import MessageEvent

    thread = _ViewThread(thread_id=800, parent_id=801,
                         category_id=MANAGED_CATEGORY)
    order = []
    _install_turn_recorder(rig, order)

    source = rig.adapter.build_source(
        chat_id="800", chat_name="forum / post", chat_type="thread",
        user_id="u1", user_name="U", thread_id="800",
        guild_id="42", parent_chat_id="801")
    event = MessageEvent(
        text="a plain pre-binding message", source=source,
        raw_message=SimpleNamespace(channel=thread, id=6000))

    async def _scenario():
        await rig.adapter.handle_message(event)
        for _key, t in list(rig.adapter._session_tasks.items()):
            if not t.done():
                await t

    asyncio.new_event_loop().run_until_complete(_scenario())

    # no binding existed → no re-home, no greeting, no seed
    assert registry.get_binding("800") is None
    assert thread.sent == []               # nothing was posted into the thread
    dispatched = [e for e in order if e["kind"] == "dispatch"]
    # the plain message DID dispatch (the hub's lane — no re-home), but to NO
    # concierge profile, and nothing about it seeded a bot: the seed + the
    # greeting ride the PROVISION step only.
    assert len(dispatched) == 1
    assert dispatched[0]["profile"] is None    # hub lane, not a bound bot
    assert dispatched[0]["session_key"].startswith("agent:None:") or \
        ":None:" not in dispatched[0]["session_key"]  # hub-namespaced lane
    assert "pre-binding" in dispatched[0]["text"]
