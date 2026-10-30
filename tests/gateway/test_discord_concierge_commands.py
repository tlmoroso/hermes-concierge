"""Concierge Discord command/event surface (Tasks 5.4/5.5/5.7 + 7.4): the
mixin's ``/bot`` handlers, managed-channel auto-offer, hub channel creation,
and the ``/room`` command surface (create/add/remove/disband).

Contracts pinned here are the REGISTRATION-TIME gates and the handler
decisions that are testable without live discord.py plumbing:
- disabled concierge → no ``/bot`` group on the tree, no auto-offer, and the
  ``on_thread_create`` chain is a pure pass-through to the normalizer;
- the auto-offer posts the SAME picker payload into a managed-category
  channel (non-ephemeral) and nothing into an unmanaged one;
- an auto-offer picker SELECTION provisions into the channel the picker was
  posted in (``prefer_create=False``/``ephemeral=False`` threaded from both
  auto-offer sites) — never a second ``#bot-<name>`` channel (the M5c
  regression);
- the MANAGE_CHANNELS pre-check receives the REAL guild id from the
  interaction, so a hub without the permission plans a bind before any API
  call (the runtime API denial stays the enforcement of last resort);
- ``/bot new``'s provision path resolves provider groups and calls
  ``provision.create_bot`` for the interaction's channel (Task 5.4), and the
  Task 5.7 fallback binds the invoking channel when channel creation is
  denied;
- a channel the hub CREATES gets a non-ephemeral greeting posted into it
  (plan 5.7: "bot says hi"), and a greeting failure never fails the
  provisioning;
- ``/bot retire`` refuses a profile the invoking guild cannot see and calls
  ``retire_bot(delete_profile=True)`` after confirmation;
- the M5c quality contracts: every handler that does slow work (the picker
  select callback, ``/bot list``, the retire confirm callback) defers
  BEFORE the service call and replies through the followup branch, and the
  slow service calls themselves (``create_bot``/``retire_bot``/
  ``bot_status``) run OFF the event-loop thread via ``asyncio.to_thread`` —
  a synchronous 60s flock wait on the loop would freeze ALL gateway event
  handling. The ~3s token expiry itself is untestable in this rig (the
  fake models no expiry); the ORDER + offload contracts are pinned instead,
  including the flock-TimeoutError path, whose failure copy must still be
  deliverable through the followup.

Real imports against a temp HERMES_HOME; discord.py objects are stubs (the
mock in tests/gateway/conftest.py shapes the module), because the live
interaction plumbing is the Task 5.6 manual E2E's job.
"""

import asyncio
import logging
import socket
import threading
import sys
import types
from types import SimpleNamespace

import pytest

from concierge import registry
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter

from tests.gateway.conftest import _ensure_discord_mock

_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter


async def _allow(*_a, **_kw):
    """Async True for gate monkeypatches."""
    return True


def _run(coro):
    """Run one handler coroutine on a fresh loop (no ambient event loop in a
    sync test)."""
    return asyncio.new_event_loop().run_until_complete(coro)


# ── the rig ────────────────────────────────────────────────────────────────


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Concierge-enabled hub adapter on a multiplexed runner, with in-hand
    config the way the boot bridge delivers it."""
    root = tmp_path
    for name in ("bot-reminders-a1b2", "bot-researcher-b2c3"):
        (root / "profiles" / name).mkdir(parents=True)
        (root / "profiles" / name / ".env").write_text("K=1\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    (root / "concierge").mkdir()

    # Live-install location artifact: gateway/run.py imports hermes_bootstrap,
    # whose MODULE-LEVEL PM activation stats the REAL ~/.hermes/manifest.json
    # when the checkout lives inside the home (a live install; CI's checkout
    # sits outside the home, so the bootstrap's activation is a no-op there) —
    # tripping tests/home_io_guard at fixture setup. Stub the module BEFORE
    # the import: the only symbol gateway.run's import chain pulls is
    # ``_happy_eyeballs_create_connection`` (agent/process_bootstrap.py),
    # satisfied with a plain socket.create_connection fallback. Same shape
    # gateway/run.py already tolerates for an unregistered bootstrap
    # (except ModuleNotFoundError).
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

    runner.config.concierge = {"enabled": True}
    return SimpleNamespace(root=root, runner=runner, adapter=adapter)


def _disable(rig):
    rig.runner.config.concierge = {"enabled": False}


class _FakeResponse:
    """``discord.InteractionResponse`` subset.

    ``defer()`` flips the ``deferred`` flag ``is_done()`` consults, so a
    deferred interaction deterministically takes ``_concierge_respond``'s
    followup branch — the real ~3s token expiry is untestable in this rig
    (the fake models no expiry), so the ORDER contract is what gets pinned
    instead: defer before the slow call, reply via followup.
    """

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
        if self._interaction is not None:
            self._interaction.order.append("defer")


class _FakeInteraction:
    """The subset of discord.Interaction the concierge handlers touch.

    ``order`` is a shared SEQUENCING log: this interaction's ``defer`` and
    ``followup`` events append to it, and tests interleave their own
    service-call markers (from monkeypatched provision fakes) into the SAME
    list to assert defer-before-work ordering. Pass one via ``order=``;
    ``followup_log`` records everything sent through the followup branch.
    """

    def __init__(self, *, channel_id="100", guild_id="42", category_id=None,
                 data=None, user_id="u1", order=None):
        self.order = order if order is not None else []
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
        self.user = SimpleNamespace(id=user_id, name="U", display_name="U")

    async def _followup_send(self, text, *, view=None, ephemeral=False):
        self.followup_log.append((text, view, ephemeral))
        self.order.append("followup")


# ── Task 5.4: /bot command registration ─────────────────────────────────────


def test_disabled_registers_no_bot_group(rig):
    """Zero-behavior-change at registration: concierge off → the tree gains no
    /bot group (the adapter's own registrations are untouched)."""
    _disable(rig)

    class _Tree:
        def __init__(self):
            self.commands = []

        def add_command(self, cmd):
            self.commands.append(cmd)

    t = _Tree()
    rig.adapter._concierge_register_slash(t)
    assert t.commands == []


def test_enabled_registers_bot_group_with_four_subcommands(rig):
    class _Tree:
        def __init__(self):
            self.commands = []

        def add_command(self, cmd):
            self.commands.append(cmd)

    t = _Tree()
    rig.adapter._concierge_register_slash(t)
    # Task 7.4: the tree carries the /bot and /room groups; Task 8.2 adds the
    # top-level /connect command after them; Task 9.2 adds /bot memory to
    # the /bot group.
    assert [getattr(c, "name", None) for c in t.commands] == [
        "bot", "room", "connect"]
    group = t.commands[0]
    assert group.name == "bot"
    assert sorted(c.name for c in group._children.values()) == [
        "list", "memory", "new", "retire"]
    connect = t.commands[2]
    assert connect.name == "connect"


def test_bot_group_survives_a_tree_conflict(rig):
    """A plugin's existing /bot group wins; the concierge group logs once and
    never raises (a registration failure must not break the whole sync)."""
    class _ConflictTree:
        def add_command(self, cmd):
            raise RuntimeError("name already registered")

    rig.adapter._concierge_register_slash(_ConflictTree())  # no raise


# ── Task 5.4: /bot new → picker → provision ─────────────────────────────────


def test_bot_new_posts_the_catalog_picker(rig, monkeypatch):
    interaction = _FakeInteraction()
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)

    _run(rig.adapter._concierge_cmd_new(interaction))

    assert len(interaction.response.sent) == 1
    _text, view, ephemeral = interaction.response.sent[0]
    assert ephemeral is True
    # The view carries the whole catalog as select options.
    select = view.children[0]
    values = [o.value for o in select.options]
    from concierge.blueprints import CATALOG

    assert values == list(CATALOG)


def test_picker_selection_provisions_the_channel(rig, monkeypatch):
    """The picker callback resolves provider groups for the interaction's
    guild/category and calls create_bot with the invoking channel."""
    calls = {}

    def fake_create_bot(blueprint_key, *, channel_id, guild_id=None,
                        provider_group=None, **kw):
        calls["blueprint"] = blueprint_key
        calls["channel"] = channel_id
        calls["guild"] = guild_id
        return {"profile": "bot-reminders-a1b2", "blueprint": blueprint_key,
                "already_provisioned": False}

    from concierge import provision

    monkeypatch.setattr(provision, "create_bot", fake_create_bot)
    rig.runner.config.concierge = {
        "enabled": True, "managed_category_ids": ["555"],
        "default_provider_group": None, "provider_groups": {},
        "hub_profile": "concierge"}

    interaction = _FakeInteraction(data={"values": ["reminders"]})
    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)
    view = rig.adapter._concierge_build_picker_view(
        [{"value": "reminders", "label": "Reminder Bot", "description": "d"}])
    select = view.children[0]

    _run(select.callback(interaction))

    assert calls["blueprint"] == "reminders"
    assert calls["channel"] == "100"
    assert calls["guild"] == "42"
    # the confirmation copy names the channel and the title; the picker
    # callback defers before provisioning, so it rides the followup branch
    text = interaction.followup_log[-1][0]
    assert "Reminder Bot" in text and "100" in text


def test_provision_failure_is_user_language(rig, monkeypatch):
    """A provisioning blow-up answers with retry copy — never a stack trace —
    and logs the detail on the hub side."""
    def _boom(*a, **kw):
        raise RuntimeError("disk on fire")

    from concierge import provision

    monkeypatch.setattr(provision, "create_bot", _boom)
    interaction = _FakeInteraction(data={"values": ["reminders"]})
    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)
    view = rig.adapter._concierge_build_picker_view(
        [{"value": "reminders", "label": "Reminder Bot", "description": "d"}])
    select = view.children[0]

    _run(select.callback(interaction))
    text = interaction.followup_log[-1][0]
    assert "try again" in text
    assert "disk on fire" not in text  # the error never leaks to the user


# ── Task 5.7: hub channel creation with fallback ───────────────────────────


def test_provision_falls_back_to_binding_when_creation_refused(rig, monkeypatch):
    """The plan's own rule: a denied channel creation binds the invoking
    channel instead — provisioning never fails over creation."""
    calls = {}

    def fake_create_bot(blueprint_key, *, channel_id, guild_id=None,
                        provider_group=None, **kw):
        calls["channel"] = channel_id
        return {"profile": "bot-reminders-a1b2", "blueprint": blueprint_key}

    from concierge import provision

    monkeypatch.setattr(provision, "create_bot", fake_create_bot)

    async def _refused_create(**kw):
        return None

    monkeypatch.setattr(
        rig.adapter, "_concierge_create_channel", _refused_create)
    rig.runner.config.concierge = {
        "enabled": True, "managed_category_ids": ["555"],
        "default_provider_group": None, "provider_groups": {},
        "hub_profile": "concierge"}

    # /bot new from a MANAGED category, creation refused → fallback binds the
    # invoking channel (never fails the provisioning).
    interaction = _FakeInteraction(channel_id="100", category_id="555")

    _run(rig.adapter._concierge_provision_from_interaction(
        interaction, "reminders", prefer_create=True))
    assert calls["channel"] == "100"  # the invoking channel was bound


def test_provision_guidance_when_nothing_to_bind(rig, monkeypatch):
    """Creation refused AND no invoking channel: guidance copy, no crash."""
    async def _refused_create(**kw):
        return None

    monkeypatch.setattr(
        rig.adapter, "_concierge_create_channel", _refused_create)
    monkeypatch.setattr(
        rig.adapter, "_concierge_can_create_channels", lambda **kw: False)
    rig.runner.config.concierge = {
        "enabled": True, "managed_category_ids": ["555"],
        "default_provider_group": None, "provider_groups": {},
        "hub_profile": "concierge"}

    interaction = _FakeInteraction(channel_id=None, category_id="555")
    interaction.channel = SimpleNamespace(id=None, category_id=555)

    _run(rig.adapter._concierge_provision_from_interaction(
        interaction, "reminders", prefer_create=True))
    text = interaction.response.sent[-1][0]
    assert "inside a channel" in text


def test_provision_binds_invoking_channel_when_denied(rig, monkeypatch):
    """Denied creation on the auto-offer path (no prefer_create): the invoking
    channel is the destination anyway — same verdict, one code path."""
    calls = {}

    def fake_create_bot(blueprint_key, *, channel_id, guild_id=None,
                        provider_group=None, **kw):
        calls["channel"] = channel_id
        return {"profile": "bot-reminders-a1b2", "blueprint": blueprint_key}

    from concierge import provision

    monkeypatch.setattr(provision, "create_bot", fake_create_bot)

    async def _refused_create(**kw):
        return None

    monkeypatch.setattr(rig.adapter, "_concierge_create_channel", _refused_create)
    monkeypatch.setattr(
        rig.adapter, "_concierge_can_create_channels",
        lambda **kw: False)
    rig.runner.config.concierge = {
        "enabled": True, "managed_category_ids": ["555"],
        "default_provider_group": None, "provider_groups": {},
        "hub_profile": "concierge"}

    interaction = _FakeInteraction(channel_id="100", category_id="555")

    _run(rig.adapter._concierge_provision_from_interaction(
        interaction, "reminders"))
    assert calls["channel"] == "100"  # bound the invoking channel


def test_created_channel_is_the_bound_one(rig, monkeypatch):
    """A successful create provisions the NEW channel, not the invoking one."""
    calls = {}

    def fake_create_bot(blueprint_key, *, channel_id, guild_id=None,
                        provider_group=None, **kw):
        calls["channel"] = channel_id
        return {"profile": "bot-reminders-a1b2", "blueprint": blueprint_key}

    from concierge import provision

    monkeypatch.setattr(provision, "create_bot", fake_create_bot)

    async def _ok_create(*, guild_id, category_id, name):
        return "9999"

    monkeypatch.setattr(rig.adapter, "_concierge_create_channel", _ok_create)
    monkeypatch.setattr(
        rig.adapter, "_concierge_can_create_channels", lambda **kw: True)
    rig.runner.config.concierge = {
        "enabled": True, "managed_category_ids": ["555"],
        "default_provider_group": None, "provider_groups": {},
        "hub_profile": "concierge"}

    # /bot new from a managed category with no channel of its own: the
    # created channel is the destination.
    interaction = _FakeInteraction(channel_id=None, category_id="555")
    interaction.channel = SimpleNamespace(id=None, category_id=555)

    _run(rig.adapter._concierge_provision_from_interaction(
        interaction, "reminders", prefer_create=True))
    assert calls["channel"] == "9999"


def test_pre_check_receives_the_real_guild_id(rig, monkeypatch):
    """The MANAGE_CHANNELS pre-check actually pre-checks: the REAL guild id
    (from the interaction) reaches _concierge_can_create_channels — the old
    wiring passed guild_id=None, which always took the unknowable→True
    branch. A hub without the permission plans a BIND before any API call
    (the runtime API-denial fallback stays the enforcement of last resort)."""
    seen = {}

    def fake_can_create(*, guild_id):
        seen["guild_id"] = guild_id
        return False  # the hub lacks MANAGE_CHANNELS

    monkeypatch.setattr(
        rig.adapter, "_concierge_can_create_channels", fake_can_create)

    calls = {}

    def fake_create_bot(blueprint_key, *, channel_id, guild_id=None,
                        provider_group=None, **kw):
        calls["channel"] = channel_id
        return {"profile": "bot-reminders-a1b2", "blueprint": blueprint_key}

    from concierge import provision

    monkeypatch.setattr(provision, "create_bot", fake_create_bot)

    async def _spy_create(**kw):
        calls["create_attempted"] = True
        return "9999"

    monkeypatch.setattr(rig.adapter, "_concierge_create_channel", _spy_create)
    rig.runner.config.concierge = {
        "enabled": True, "managed_category_ids": ["555"],
        "default_provider_group": None, "provider_groups": {},
        "hub_profile": "concierge"}

    # /bot new's picker (prefer_create=True) in a managed category: the
    # denied pre-check plans a bind — no create attempt ever goes out.
    interaction = _FakeInteraction(
        channel_id="100", guild_id="42", category_id="555",
        data={"values": ["reminders"]})
    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)
    view = rig.adapter._concierge_build_picker_view(
        [{"value": "reminders", "label": "Reminder Bot", "description": "d"}])

    _run(view.children[0].callback(interaction))

    assert seen["guild_id"] == "42"   # the REAL guild id reached the pre-check
    assert calls["channel"] == "100"  # denied pre-check → bind, no create
    assert "create_attempted" not in calls


def test_created_channel_gets_a_greeting(rig, monkeypatch):
    """Plan 5.7 ("channel appears, bot says hi"): a /bot new selection in a
    managed category creates the dedicated channel, provisions into it, AND
    posts a non-ephemeral greeting into the new channel — not just the
    ephemeral interaction reply."""
    calls = {}

    def fake_create_bot(blueprint_key, *, channel_id, guild_id=None,
                        provider_group=None, **kw):
        calls["channel"] = channel_id
        return {"profile": "bot-reminders-a1b2", "blueprint": blueprint_key}

    from concierge import provision

    monkeypatch.setattr(provision, "create_bot", fake_create_bot)

    async def _ok_create(*, guild_id, category_id, name):
        calls["created_name"] = name
        return "9999"

    monkeypatch.setattr(rig.adapter, "_concierge_create_channel", _ok_create)

    greeted = []

    class _NewChannel:
        async def send(self, text, **kw):
            greeted.append((text, kw))

    monkeypatch.setattr(
        rig.adapter, "_client",
        SimpleNamespace(get_channel=lambda cid: (
            _NewChannel() if int(cid) == 9999 else None)),
        raising=False)
    rig.runner.config.concierge = {
        "enabled": True, "managed_category_ids": ["555"],
        "default_provider_group": None, "provider_groups": {},
        "hub_profile": "concierge"}

    # /bot new's picker keeps the defaults: prefer_create=True (a dedicated
    # channel), ephemeral replies.
    interaction = _FakeInteraction(
        channel_id="100", category_id="555", data={"values": ["reminders"]})
    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)
    view = rig.adapter._concierge_build_picker_view(
        [{"value": "reminders", "label": "Reminder Bot", "description": "d"}])

    _run(view.children[0].callback(interaction))

    assert calls["channel"] == "9999"            # provisioned INTO the new channel
    assert calls["created_name"] == "bot-reminder"
    assert len(greeted) == 1                     # the greeting landed in #9999
    text, kw = greeted[0]
    assert "Reminder Bot" in text and "say hi" in text
    assert not kw.get("ephemeral")               # a plain (public) channel post
    # the picker deferred before provisioning: the reply rode the followup
    reply, _v, ephemeral = interaction.followup_log[-1]
    assert "9999" in reply and ephemeral is True  # the reply stays ephemeral


def test_greeting_failure_never_fails_provisioning(rig, monkeypatch):
    """A greeting that blows up (channel deleted under us, send denied) is
    logged and swallowed — the provisioning itself already succeeded."""
    calls = {}

    def fake_create_bot(blueprint_key, *, channel_id, guild_id=None,
                        provider_group=None, **kw):
        calls["channel"] = channel_id
        return {"profile": "bot-reminders-a1b2", "blueprint": blueprint_key}

    from concierge import provision

    monkeypatch.setattr(provision, "create_bot", fake_create_bot)

    async def _ok_create(**kw):
        return "9999"

    monkeypatch.setattr(rig.adapter, "_concierge_create_channel", _ok_create)

    class _Unsendable:
        async def send(self, *a, **kw):
            raise RuntimeError("cannot send messages here")

    monkeypatch.setattr(
        rig.adapter, "_client",
        SimpleNamespace(get_channel=lambda cid: _Unsendable()),
        raising=False)
    rig.runner.config.concierge = {
        "enabled": True, "managed_category_ids": ["555"],
        "default_provider_group": None, "provider_groups": {},
        "hub_profile": "concierge"}

    interaction = _FakeInteraction(channel_id="100", category_id="555")

    _run(rig.adapter._concierge_provision_from_interaction(
        interaction, "reminders", prefer_create=True))  # no raise
    assert calls["channel"] == "9999"
    text = interaction.response.sent[-1][0]
    assert "9999" in text  # the confirmation still went out


# ── Task 5.5: auto-offer on channel create ─────────────────────────────────


class _FakeChannel:
    def __init__(self, *, channel_id, category_id, sends):
        self.id = int(channel_id)
        self.category_id = int(category_id) if category_id else None
        self._sends = sends

    async def send(self, text, *, view=None):
        self._sends.append((str(self.id), text, view))


def test_auto_offer_posts_picker_into_managed_channel(rig):
    sends = []
    channel = _FakeChannel(channel_id="700", category_id="555", sends=sends)
    rig.runner.config.concierge = {
        "enabled": True, "managed_category_ids": ["555"],
        "default_provider_group": None, "provider_groups": {},
        "hub_profile": "concierge"}

    _run(rig.adapter._concierge_on_guild_channel_create(channel))

    assert len(sends) == 1
    chan_id, _text, view = sends[0]
    assert chan_id == "700"
    # Non-ephemeral (channel.send has no ephemeral at all) and the whole
    # catalog is on the picker.
    assert [o.value for o in view.children[0].options] == [
        "reminders", "scheduler", "researcher", "email_triage", "notes"]


def test_auto_offer_selection_binds_the_posted_in_channel(rig, monkeypatch):
    """The M5c regression: an auto-offer selection must provision into the
    channel the picker was POSTED IN — never create a second #bot-<name>
    channel and leave the user's fresh one empty. Drives the REAL production
    path: on_guild_channel_create posts the picker, then the production
    select callback runs (no direct _concierge_provision_from_interaction
    call, no overridden defaults)."""
    sends = []
    channel = _FakeChannel(channel_id="700", category_id="555", sends=sends)
    rig.runner.config.concierge = {
        "enabled": True, "managed_category_ids": ["555"],
        "default_provider_group": None, "provider_groups": {},
        "hub_profile": "concierge"}

    # 1) the production event handler posts the picker into the new channel
    _run(rig.adapter._concierge_on_guild_channel_create(channel))
    assert len(sends) == 1
    view = sends[0][2]

    # 2) channel-creation spy: the create path must NEVER run (a managed
    #    category is exactly where the old bug planned a second channel)
    create_calls = []

    async def _spy_create(**kw):
        create_calls.append(kw)
        return "9999"

    monkeypatch.setattr(rig.adapter, "_concierge_create_channel", _spy_create)

    def fake_create_bot(blueprint_key, *, channel_id, guild_id=None,
                        provider_group=None, **kw):
        # mirror the real contract: the binding lands in the registry
        registry.bind_channel(
            str(channel_id), profile="bot-reminders-a1b2",
            blueprint=blueprint_key, guild_id=guild_id)
        return {"profile": "bot-reminders-a1b2", "blueprint": blueprint_key,
                "already_provisioned": False}

    from concierge import provision

    monkeypatch.setattr(provision, "create_bot", fake_create_bot)
    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)

    # 3) the user picks a bot from the picker posted in channel 700
    interaction = _FakeInteraction(
        channel_id="700", guild_id="42", category_id="555",
        data={"values": ["reminders"]})

    _run(view.children[0].callback(interaction))

    # the posted-in channel is the one bound — the REGISTRY's verdict
    binding = registry.get_binding("700")
    assert binding is not None
    assert binding["profile"] == "bot-reminders-a1b2"
    assert binding["blueprint"] == "reminders"
    # NO second channel: the create path never ran, nothing bound elsewhere
    assert create_calls == []
    assert registry.get_binding("9999") is None
    # the confirmation is public and names the posted-in channel; the select
    # callback defers NON-ephemerally on the auto-offer path (the picker POST
    # itself is a channel send — no interaction to defer)
    assert interaction.response.defer_ephemeral == [False]
    text, _view, ephemeral = interaction.followup_log[-1]
    assert "700" in text
    assert ephemeral is False


def test_auto_offer_skips_unmanaged_category(rig):
    sends = []
    channel = _FakeChannel(channel_id="700", category_id="900", sends=sends)
    rig.runner.config.concierge = {
        "enabled": True, "managed_category_ids": ["555"],
        "default_provider_group": None, "provider_groups": {},
        "hub_profile": "concierge"}

    _run(rig.adapter._concierge_on_guild_channel_create(channel))
    assert sends == []


def test_auto_offer_disabled_means_no_picker(rig):
    sends = []
    channel = _FakeChannel(channel_id="700", category_id="555", sends=sends)
    _disable(rig)

    _run(rig.adapter._concierge_on_guild_channel_create(channel))
    assert sends == []


def test_auto_offer_survives_a_send_failure(rig):
    """A broadcast event handler never raises (one channel's picker failure
    must not affect anything else listening)."""
    class _Exploding:
        id = 701
        category_id = 555

        async def send(self, *a, **kw):
            raise RuntimeError("cannot send announcements")

    rig.runner.config.concierge = {
        "enabled": True, "managed_category_ids": ["555"],
        "default_provider_group": None, "provider_groups": {},
        "hub_profile": "concierge"}

    _run(rig.adapter._concierge_on_guild_channel_create(_Exploding()))  # no raise


# ── channel-delete auto-teardown ───────────────────────────────────────────


def test_channel_delete_retires_and_deletes_a_last_channel_bot(rig, monkeypatch):
    """A deleted channel bound to a bot with NO other channels retires the
    bot with delete_profile=True — a bot with nowhere left to answer is an
    orphan no retire picker can list."""
    registry.bind_channel("700", profile="bot-reminders-a1b2",
                          blueprint="reminders")
    retired = []

    def fake_retire(profile, *, delete_profile=False):
        retired.append((profile, delete_profile))
        # The real retire_bot's own contract: unbind LAST.
        registry.unbind_channel("700")

    from concierge import provision

    monkeypatch.setattr(provision, "retire_bot", fake_retire)

    _run(rig.adapter._concierge_on_guild_channel_delete(
        SimpleNamespace(id=700)))

    assert retired == [("bot-reminders-a1b2", True)]
    assert registry.get_binding("700") is None


def test_channel_delete_keeps_a_bot_with_other_channels(rig, monkeypatch):
    """A bot bound to several channels loses only the deleted channel's
    binding — it still answers elsewhere, so its profile stays."""
    registry.bind_channel("700", profile="bot-reminders-a1b2",
                          blueprint="reminders")
    registry.bind_channel("701", profile="bot-reminders-a1b2",
                          blueprint="reminders")
    retired = []

    from concierge import provision

    monkeypatch.setattr(
        provision, "retire_bot",
        lambda profile, *, delete_profile=False: retired.append(profile))

    _run(rig.adapter._concierge_on_guild_channel_delete(
        SimpleNamespace(id=700)))

    assert retired == []
    assert registry.get_binding("700") is None
    assert registry.get_binding("701") is not None


def test_channel_delete_disbands_a_room_without_touching_members(rig,
                                                                 monkeypatch):
    """A deleted ROOM channel tears down the room key only — member bots
    keep their own bindings and profiles."""
    registry.bind_channel("800", profile="bot-reminders-a1b2",
                          blueprint="reminders")
    registry.create_room("900", ["bot-reminders-a1b2", "bot-researcher-b2c3"])
    retired = []

    from concierge import provision

    monkeypatch.setattr(
        provision, "retire_bot",
        lambda profile, *, delete_profile=False: retired.append(profile))

    _run(rig.adapter._concierge_on_guild_channel_delete(
        SimpleNamespace(id=900)))

    assert retired == []
    assert registry.room_members("900") == []
    assert registry.get_binding("800") is not None


def test_channel_delete_ignores_unbound_and_disabled(rig):
    """An unbound channel is a no-op, and a disabled concierge never touches
    the registry (same gate as the create-side auto-offer)."""
    registry.bind_channel("700", profile="bot-reminders-a1b2",
                          blueprint="reminders")

    _run(rig.adapter._concierge_on_guild_channel_delete(
        SimpleNamespace(id=999)))
    assert registry.get_binding("700") is not None

    _disable(rig)
    _run(rig.adapter._concierge_on_guild_channel_delete(
        SimpleNamespace(id=700)))
    assert registry.get_binding("700") is not None


def test_channel_delete_survives_a_teardown_failure(rig, monkeypatch):
    """A broadcast event handler never raises — even when the retire blows
    up mid-teardown (a retry finishes the job: retire_bot's own order)."""
    registry.bind_channel("700", profile="bot-reminders-a1b2",
                          blueprint="reminders")

    from concierge import provision

    def boom(profile, *, delete_profile=False):
        raise RuntimeError("teardown exploded")

    monkeypatch.setattr(provision, "retire_bot", boom)
    _run(rig.adapter._concierge_on_guild_channel_delete(
        SimpleNamespace(id=700)))  # no raise


# ── Task 5.5: on_thread_create chaining ────────────────────────────────────


def test_thread_chain_passes_through_when_disabled(rig):
    _disable(rig)
    seen = []

    async def _normalize(th):
        seen.append(th)

    rig.adapter._on_platform_thread_create = _normalize
    thread = SimpleNamespace(id=5, parent=SimpleNamespace(id=1, category_id=555))
    _run(rig.adapter._concierge_on_thread_create(thread))
    assert seen == [thread]


def test_thread_chain_offers_then_normalizes_in_managed_forum(rig):
    sends = []

    class _Thread:
        id = 5

        def __init__(self):
            self.parent = SimpleNamespace(id=1, category_id=555)

        async def send(self, text, *, view=None):
            sends.append((text, view))

    thread = _Thread()
    seen = []

    async def _normalize(th):
        seen.append(th)

    rig.adapter._on_platform_thread_create = _normalize
    rig.runner.config.concierge = {
        "enabled": True, "managed_category_ids": ["555"],
        "default_provider_group": None, "provider_groups": {},
        "hub_profile": "concierge"}

    _run(rig.adapter._concierge_on_thread_create(thread))
    assert seen == [thread]      # the normalizer ALWAYS runs
    assert len(sends) == 1        # and the picker was offered (managed forum)


def test_thread_chain_normalizes_without_offer_in_unmanaged_forum(rig):
    sends = []

    class _Thread:
        id = 5

        def __init__(self):
            self.parent = SimpleNamespace(id=1, category_id=900)

        async def send(self, text, *, view=None):
            sends.append((text, view))

    thread = _Thread()
    seen = []

    async def _normalize(th):
        seen.append(th)

    rig.adapter._on_platform_thread_create = _normalize
    rig.runner.config.concierge = {
        "enabled": True, "managed_category_ids": ["555"],
        "default_provider_group": None, "provider_groups": {},
        "hub_profile": "concierge"}

    _run(rig.adapter._concierge_on_thread_create(thread))
    assert seen == [thread]
    assert sends == []


def test_thread_chain_survives_picker_failure(rig):
    """The auto-offer half is wrapped: a picker blow-up must never take the
    normalizer down with it (the platform-event pipeline depends on it)."""
    class _ExplodingThread:
        id = 5
        parent = SimpleNamespace(id=1, category_id=555)

        async def send(self, *a, **kw):
            raise RuntimeError("nope")

    seen = []

    async def _normalize(th):
        seen.append(th)

    rig.adapter._on_platform_thread_create = _normalize
    rig.runner.config.concierge = {
        "enabled": True, "managed_category_ids": ["555"],
        "default_provider_group": None, "provider_groups": {},
        "hub_profile": "concierge"}

    _run(rig.adapter._concierge_on_thread_create(_ExplodingThread()))
    assert len(seen) == 1  # the normalizer ran despite the picker failure


# ── Task 5.4: /bot list ─────────────────────────────────────────────────────


def test_bot_lists_this_guilds_bots(rig, monkeypatch):
    registry.bind_channel("100", profile="bot-reminders-a1b2",
                          blueprint="reminders", guild_id="42")
    registry.bind_channel("300", profile="bot-researcher-b2c3",
                          blueprint="researcher", guild_id="77")

    interaction = _FakeInteraction(guild_id="42")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction",
        _allow)

    _run(rig.adapter._concierge_cmd_list(interaction))
    # /bot list defers before the status walk → followup branch
    text = interaction.followup_log[-1][0]
    assert "Reminder Bot" in text
    assert "Researcher Bot" not in text  # guild 77's


def test_bot_list_empty_copy(rig, monkeypatch):
    interaction = _FakeInteraction(guild_id="42")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction",
        _allow)

    _run(rig.adapter._concierge_cmd_list(interaction))
    text = interaction.followup_log[-1][0]
    assert "No bots here yet" in text


# ── Task 9.2: /bot memory — the shared-workspace report ─────────────────────


class _StubDialecticClient:
    """The HonchoSessionManager query surface, stubbed (no network) — the
    same shape tests/concierge/test_memory_report.py stubs."""

    def get_or_create(self, key, **_kw):
        return object()

    def dialectic_query(self, session_key, query, **_kw):
        return "You prefer short answers and work in Pacific time."

    def get_peer_card(self, session_key, peer="user"):
        return ["Prefers concise replies"]

    def list_conclusions(self, session_key, query=None, peer="user", limit=20):
        return [{"id": "c1", "content": "Wants at most two pings a day"}]


def test_bot_memory_returns_the_formatted_report(rig, monkeypatch):
    """The command path with a STUBBED client factory: the report built from
    the stub's workspace knowledge lands as an ephemeral followup."""
    from concierge import memory_report

    monkeypatch.setattr(rig.adapter, "_concierge_gate_interaction", _allow)
    seen = {}

    def _factory(**kw):
        seen.update(kw)
        return _StubDialecticClient()

    monkeypatch.setattr(memory_report, "memory_report_client", _factory)
    interaction = _FakeInteraction()

    _run(rig.adapter._concierge_cmd_memory(interaction))

    text, _view, ephemeral = interaction.followup_log[-1]
    assert ephemeral is True
    assert "You prefer short answers and work in Pacific time." in text
    assert "Prefers concise replies" in text
    assert "Wants at most two pings a day" in text
    # the client is built with the INVOKING user's id — the runtime identity
    # that resolves the user peer, exactly like a gateway turn would
    assert seen["user_id"] == "u1"


def test_bot_memory_defers_before_the_network_work(rig, monkeypatch):
    """The M5c ORDER contract, /bot memory edition: defer() precedes the
    client build (network I/O), the reply rides the followup branch."""
    from concierge import memory_report

    monkeypatch.setattr(rig.adapter, "_concierge_gate_interaction", _allow)
    order = []

    def _factory(**_kw):
        order.append("build")
        return _StubDialecticClient()

    monkeypatch.setattr(memory_report, "memory_report_client", _factory)
    interaction = _FakeInteraction(order=order)

    _run(rig.adapter._concierge_cmd_memory(interaction))

    assert order == ["defer", "build", "followup"]


def test_bot_memory_failure_is_the_failure_copy(rig, monkeypatch):
    """A client-construction blow-up (Honcho down, unconfigured, auth
    rejected) answers with the failure copy — never a traceback, never the
    internal detail."""
    from concierge import memory_report

    monkeypatch.setattr(rig.adapter, "_concierge_gate_interaction", _allow)

    def _boom(**_kw):
        raise RuntimeError("connection refused: honcho backend down")

    monkeypatch.setattr(memory_report, "memory_report_client", _boom)
    interaction = _FakeInteraction()

    _run(rig.adapter._concierge_cmd_memory(interaction))

    text, _view, ephemeral = interaction.followup_log[-1]
    assert ephemeral is True
    assert text == memory_report.FAILURE_COPY
    assert "honcho backend down" not in text


def test_bot_memory_empty_workspace_is_the_empty_copy(rig, monkeypatch):
    """An honest nothing-know-yet is not an error: the empty workspace copy
    flows through the command path too."""

    class _EmptyClient:
        def get_or_create(self, key, **_kw):
            return object()

        def dialectic_query(self, session_key, query, **_kw):
            return ""

        def get_peer_card(self, session_key, peer="user"):
            return []

        def list_conclusions(self, session_key, query=None, peer="user",
                            limit=20):
            return []

    from concierge import memory_report

    monkeypatch.setattr(rig.adapter, "_concierge_gate_interaction", _allow)
    monkeypatch.setattr(memory_report, "memory_report_client",
                        lambda **_kw: _EmptyClient())
    interaction = _FakeInteraction()

    _run(rig.adapter._concierge_cmd_memory(interaction))

    assert interaction.followup_log[-1][0] == memory_report.EMPTY_COPY


# ── Task 5.4: /bot retire ────────────────────────────────────────────────────


def test_retire_refuses_another_guilds_bot(rig, monkeypatch):
    registry.bind_channel("300", profile="bot-researcher-b2c3",
                          blueprint="researcher", guild_id="77")
    interaction = _FakeInteraction(guild_id="42")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction",
        _allow)

    _run(rig.adapter._concierge_cmd_retire(interaction, "bot-researcher-b2c3"))
    text = interaction.response.sent[-1][0]
    assert "isn't one of this server's bots" in text


def test_retire_confirm_button_deletes_profile(rig, monkeypatch):
    registry.bind_channel("100", profile="bot-reminders-a1b2",
                          blueprint="reminders", guild_id="42")
    retired = {}

    def fake_retire(profile, *, delete_profile=False):
        retired["profile"] = profile
        retired["delete"] = delete_profile

    from concierge import provision

    monkeypatch.setattr(provision, "retire_bot", fake_retire)
    interaction = _FakeInteraction(guild_id="42", user_id="u1")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction",
        _allow)
    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)

    _run(rig.adapter._concierge_cmd_retire(interaction, "bot-reminders-a1b2"))
    # the confirm view went up
    _, view, _ = interaction.response.sent[-1]
    assert len(view.children) == 2  # Retire + Cancel
    retire_btn = view.children[0]
    click = _FakeInteraction(guild_id="42", user_id="u1")

    _run(retire_btn.callback(click))
    assert retired == {"profile": "bot-reminders-a1b2", "delete": True}
    # the confirm callback defers before the teardown → followup branch
    text = click.followup_log[-1][0]
    assert "Retired" in text


def test_retire_cancel_leaves_everything(rig, monkeypatch):
    registry.bind_channel("100", profile="bot-reminders-a1b2",
                          blueprint="reminders", guild_id="42")
    interaction = _FakeInteraction(guild_id="42")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction",
        _allow)
    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)

    _run(rig.adapter._concierge_cmd_retire(interaction, "bot-reminders-a1b2"))
    _, view, _ = interaction.response.sent[-1]
    cancel_btn = view.children[1]
    click = _FakeInteraction(guild_id="42")

    _run(cancel_btn.callback(click))
    text = click.response.sent[-1][0]
    assert "Cancelled" in text
    # nothing was retired: the binding still stands
    assert registry.get_binding("100") is not None


# ── M5c quality fix: defer before slow work, service calls off the loop ─────


def _picker_callback(rig):
    """A production ``/bot new`` picker select callback (ephemeral replies,
    ``prefer_create=True`` defaults)."""
    view = rig.adapter._concierge_build_picker_view(
        [{"value": "reminders", "label": "Reminder Bot", "description": "d"}])
    return view.children[0].callback


def test_picker_defers_before_create_bot(rig, monkeypatch):
    """ORDER contract (picker select callback): defer() precedes the
    create_bot call, and the confirmation rides the followup branch. The
    ~3s token expiry is untestable here — the fake models no expiry — so
    the sequence is the pinned contract."""
    order = []

    def fake_create_bot(blueprint_key, *, channel_id, guild_id=None,
                        provider_group=None, **kw):
        order.append("create_bot")
        return {"profile": "bot-reminders-a1b2", "blueprint": blueprint_key,
                "already_provisioned": False}

    from concierge import provision

    monkeypatch.setattr(provision, "create_bot", fake_create_bot)
    interaction = _FakeInteraction(data={"values": ["reminders"]}, order=order)
    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)

    _run(_picker_callback(rig)(interaction))

    assert order.index("defer") < order.index("create_bot")
    # deferred → the reply went through the followup, never a second response
    assert interaction.followup_log
    assert interaction.response.sent == []


def test_retire_confirm_defers_before_retire_bot(rig, monkeypatch):
    """ORDER contract (retire confirm callback): defer() precedes the
    retire_bot call; the outcome copy rides the followup branch."""
    order = []
    registry.bind_channel("100", profile="bot-reminders-a1b2",
                          blueprint="reminders", guild_id="42")

    def fake_retire(profile, *, delete_profile=False):
        order.append("retire_bot")

    from concierge import provision

    monkeypatch.setattr(provision, "retire_bot", fake_retire)
    interaction = _FakeInteraction(guild_id="42", user_id="u1")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)
    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)

    _run(rig.adapter._concierge_cmd_retire(interaction, "bot-reminders-a1b2"))
    # the command itself answers fast (the confirm view, no defer yet)…
    _, view, _ = interaction.response.sent[-1]
    click = _FakeInteraction(guild_id="42", user_id="u1", order=order)

    _run(view.children[0].callback(click))  # …the BUTTON does the slow work

    assert order.index("defer") < order.index("retire_bot")
    assert click.followup_log
    assert click.response.sent == []


def test_bot_list_defers_before_the_status_walk(rig, monkeypatch):
    """ORDER contract (/bot list): defer() precedes the bot_status walk (the
    cron-store reads behind the list rows); the rows ride the followup."""
    order = []
    registry.bind_channel("100", profile="bot-reminders-a1b2",
                          blueprint="reminders", guild_id="42")

    def fake_status(profile_name):
        order.append("bot_status")
        return {"routines": []}

    from concierge import provision

    monkeypatch.setattr(provision, "bot_status", fake_status)
    interaction = _FakeInteraction(guild_id="42", order=order)
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)

    _run(rig.adapter._concierge_cmd_list(interaction))

    assert order.index("defer") < order.index("bot_status")
    assert interaction.followup_log
    assert interaction.response.sent == []


def test_create_bot_runs_off_the_event_loop(rig, monkeypatch):
    """OFFLOAD contract: create_bot runs on a worker thread, never the
    discord.py event-loop thread — a synchronous 60s flock wait on the loop
    freezes ALL gateway event handling and misses the WS heartbeat. The
    fake records the thread it ran on; the loop runs on THIS thread (see
    ``_run``), so the two idents must differ."""
    seen = {}

    def fake_create_bot(blueprint_key, *, channel_id, guild_id=None,
                        provider_group=None, **kw):
        seen["thread"] = threading.get_ident()
        return {"profile": "bot-reminders-a1b2", "blueprint": blueprint_key}

    from concierge import provision

    monkeypatch.setattr(provision, "create_bot", fake_create_bot)
    interaction = _FakeInteraction(data={"values": ["reminders"]})
    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)

    _run(_picker_callback(rig)(interaction))

    assert seen["thread"] != threading.get_ident()  # off the loop thread


def test_retire_bot_runs_off_the_event_loop(rig, monkeypatch):
    """OFFLOAD contract: retire_bot (cron-store pause, profile deletion)
    runs on a worker thread."""
    seen = {}

    def fake_retire(profile, *, delete_profile=False):
        seen["thread"] = threading.get_ident()

    from concierge import provision

    monkeypatch.setattr(provision, "retire_bot", fake_retire)
    registry.bind_channel("100", profile="bot-reminders-a1b2",
                          blueprint="reminders", guild_id="42")
    interaction = _FakeInteraction(guild_id="42", user_id="u1")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)
    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)

    _run(rig.adapter._concierge_cmd_retire(interaction, "bot-reminders-a1b2"))
    _, view, _ = interaction.response.sent[-1]
    click = _FakeInteraction(guild_id="42", user_id="u1")

    _run(view.children[0].callback(click))

    assert seen["thread"] != threading.get_ident()


def test_bot_status_runs_off_the_event_loop(rig, monkeypatch):
    """OFFLOAD contract: /bot list's bot_status cron-store walk (inside the
    offloaded payload build) runs on a worker thread."""
    seen = {}

    def fake_status(profile_name):
        seen["thread"] = threading.get_ident()
        return {"routines": []}

    from concierge import provision

    monkeypatch.setattr(provision, "bot_status", fake_status)
    registry.bind_channel("100", profile="bot-reminders-a1b2",
                          blueprint="reminders", guild_id="42")
    interaction = _FakeInteraction(guild_id="42")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)

    _run(rig.adapter._concierge_cmd_list(interaction))

    assert seen["thread"] != threading.get_ident()


def test_provision_lock_timeout_still_replies_via_followup(rig, monkeypatch):
    """The flock-TimeoutError path: with the defer FIRST the failure copy is
    still deliverable — defer, (failed) work, followup. Before the fix the
    burned token swallowed even the failure copy at DEBUG."""
    order = []

    def _locked_out(*a, **kw):
        raise TimeoutError("concierge provision lock busy")

    from concierge import provision

    monkeypatch.setattr(provision, "create_bot", _locked_out)
    interaction = _FakeInteraction(data={"values": ["reminders"]}, order=order)
    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)

    _run(_picker_callback(rig)(interaction))

    # defer → (failed) work → followup: the retry copy WAS delivered
    assert order == ["defer", "followup"]
    text = interaction.followup_log[-1][0]
    assert "try again" in text


# ── Task 7.4: /room slash commands ──────────────────────────────────────────


def test_enabled_registers_room_group_with_four_subcommands(rig):
    class _Tree:
        def __init__(self):
            self.commands = []

        def add_command(self, cmd):
            self.commands.append(cmd)

    t = _Tree()
    rig.adapter._concierge_register_slash(t)
    # Task 8.2: the top-level /connect command follows the two groups
    assert [c.name for c in t.commands] == ["bot", "room", "connect"]
    room = t.commands[1]
    assert sorted(c.name for c in room._children.values()) == [
        "add", "create", "disband", "remove"]


def test_room_group_survives_a_tree_conflict(rig):
    """A plugin's existing /room group wins; the concierge group logs once
    and never raises (and the /bot group registration is unaffected)."""
    added = []

    class _ConflictTree:
        def add_command(self, cmd):
            added.append(cmd.name)
            if cmd.name == "room":
                raise RuntimeError("name already registered")

    rig.adapter._concierge_register_slash(_ConflictTree())  # no raise
    assert added == ["bot", "room", "connect"]


def _bind_guild_bots(rig, *profiles, guild="42"):
    for i, profile in enumerate(profiles):
        registry.bind_channel(
            str(500 + i), profile=profile,
            blueprint=profile.split("-")[1] if "-" in profile else "reminders",
            guild_id=guild)
    return [str(500 + i) for i in range(len(profiles))]


def _room_picker_from_command(rig, monkeypatch, interaction):
    """Drive the REAL ``/room create`` handler and return the picker view it
    posted (the production path — no direct view construction)."""
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)
    _run(rig.adapter._concierge_cmd_room_create(interaction, None))
    _text, view, _eph = interaction.response.sent[-1]
    return view


def test_room_create_happy_path(rig, monkeypatch):
    """2 bots → the hub creates #room-<topic> (Task 5.7 create path), the
    registry stores the room on the NEW channel with the picked order, the
    reply names the channel, and a public greeting naming the members lands
    IN the room channel. Seven bound bots so the picker's 2–6 multi-select
    bounds are exercised against a full options list."""
    _bind_guild_bots(rig, "bot-reminders-a1b2", "bot-researcher-b2c3",
                     *[f"bot-fill{i}-f{i}f{i}" for i in range(5)])
    created = {}

    async def _ok_create(*, guild_id, category_id, name):
        created["name"] = name
        return "9999"

    monkeypatch.setattr(rig.adapter, "_concierge_create_channel", _ok_create)
    greeted = []

    class _RoomChannel:
        async def send(self, text, **kw):
            greeted.append((text, kw))

    monkeypatch.setattr(
        rig.adapter, "_client",
        SimpleNamespace(get_channel=lambda cid: (
            _RoomChannel() if int(cid) == 9999 else None)),
        raising=False)

    interaction = _FakeInteraction(channel_id="100", guild_id="42")
    view = _room_picker_from_command(rig, monkeypatch, interaction)
    select = view.children[0]
    assert select.min_values == 2 and select.max_values == 6  # multi-select

    pick = _FakeInteraction(
        channel_id="100", guild_id="42",
        data={"values": ["bot-researcher-b2c3", "bot-reminders-a1b2"]})
    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)

    _run(select.callback(pick))

    # the room lives on the CREATED channel, members in PICKED order
    assert registry.room_members("9999") == [
        "bot-researcher-b2c3", "bot-reminders-a1b2"]
    assert created["name"] == "room"  # no topic given: the plain name
    # the invoking channel was NOT turned into a room (or a binding)
    assert registry.room_members("100") == []
    assert registry.get_binding("100") is None
    # the reply names the new channel and its members (titles here are the
    # profile names — profile.yaml carries no description in this rig)
    reply = pick.followup_log[-1][0]
    assert "9999" in reply and "bot-reminders-a1b2" in reply
    # the public greeting landed in the room channel, naming the members
    assert len(greeted) == 1
    text, kw = greeted[0]
    assert "bot-researcher-b2c3" in text and "bot-reminders-a1b2" in text
    assert not kw.get("ephemeral")


def test_room_create_topics_the_channel_name(rig, monkeypatch):
    _bind_guild_bots(rig, "bot-reminders-a1b2", "bot-researcher-b2c3")
    created = {}

    async def _ok_create(*, guild_id, category_id, name):
        created["name"] = name
        return "9999"

    monkeypatch.setattr(rig.adapter, "_concierge_create_channel", _ok_create)

    # drive the command WITH a topic
    interaction = _FakeInteraction(channel_id="100", guild_id="42")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)
    _run(rig.adapter._concierge_cmd_room_create(interaction, "Trip Planning"))
    _text, view, _eph = interaction.response.sent[-1]
    pick = _FakeInteraction(
        channel_id="100", guild_id="42",
        data={"values": ["bot-reminders-a1b2", "bot-researcher-b2c3"]})
    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)

    _run(view.children[0].callback(pick))

    assert created["name"] == "room-trip-planning"
    assert registry.room_members("9999") == [
        "bot-reminders-a1b2", "bot-researcher-b2c3"]


def test_room_create_refuses_with_fewer_than_two_guild_bots(rig, monkeypatch):
    """No picker at all when the guild has <2 bound bots — actionable copy."""
    _bind_guild_bots(rig, "bot-reminders-a1b2")  # just one
    interaction = _FakeInteraction(channel_id="100", guild_id="42")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)

    _run(rig.adapter._concierge_cmd_room_create(interaction, None))

    text = interaction.response.sent[-1][0]
    assert "at least two bots" in text
    assert "`/bot new`" in text


def test_room_create_selection_refusals(rig, monkeypatch):
    """The pure validator's verdicts surface verbatim: 1 bot, >6 bots, an
    unbound bot — nothing is created, nothing stored."""
    _bind_guild_bots(
        rig, *[f"bot-x{i}-aaaa" for i in range(7)])
    for name in ("bot-x0-aaaa", "bot-x1-aaaa"):
        (rig.root / "profiles" / name).mkdir(parents=True, exist_ok=True)
    create_calls = []

    async def _spy_create(**kw):
        create_calls.append(kw)
        return "9999"

    monkeypatch.setattr(rig.adapter, "_concierge_create_channel", _spy_create)
    interaction = _FakeInteraction(channel_id="100", guild_id="42")
    view = _room_picker_from_command(rig, monkeypatch, interaction)
    select = view.children[0]
    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)

    cases = [
        (["bot-x0-aaaa"], "at least two"),
        ([f"bot-x{i}-aaaa" for i in range(7)], "at most 6"),
        (["bot-x0-aaaa", "bot-stranger-9999"], "isn't one of this server's bots"),
    ]
    for values, expected in cases:
        pick = _FakeInteraction(
            channel_id="100", guild_id="42", data={"values": values})
        _run(select.callback(pick))
        text = pick.followup_log[-1][0]
        assert expected in text, f"{values!r} -> {text!r}"

    assert create_calls == []  # nothing was ever created
    assert registry.room_members("9999") == []


def test_room_create_refuses_when_channel_creation_denied(rig, monkeypatch):
    """A room NEEDS its own channel: unlike /bot new there is NO bind
    fallback (the live 403 'Missing Permissions (50013)' path) — the refusal
    is EPHEMERAL, carries the grant-me-the-permission guidance, and aborts
    with the registry untouched (no partial room)."""
    _bind_guild_bots(rig, "bot-reminders-a1b2", "bot-researcher-b2c3")
    before = registry.load_registry()

    async def _refused_create(**kw):
        return None

    monkeypatch.setattr(rig.adapter, "_concierge_create_channel", _refused_create)
    interaction = _FakeInteraction(channel_id="100", guild_id="42")
    view = _room_picker_from_command(rig, monkeypatch, interaction)
    pick = _FakeInteraction(
        channel_id="100", guild_id="42",
        data={"values": ["bot-reminders-a1b2", "bot-researcher-b2c3"]})
    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)

    _run(view.children[0].callback(pick))

    text, _view, ephemeral = pick.followup_log[-1]
    assert text == (
        "I couldn't create a channel — grant me the **Manage Channels** "
        "permission (Server Settings → Roles), then run /room create again.")
    assert ephemeral is True
    assert registry.room_members("100") == []  # NOT bound as a room
    assert registry.get_binding("100") is None  # and not as a binding either
    assert registry.load_registry() == before  # no partial room anywhere


def test_room_add_round_trip(rig, monkeypatch):
    """``/room add`` on the room of the INVOKING channel: a guild-scoped
    bound bot joins (registry mutation, real), the reply confirms with the
    new membership line."""
    _bind_guild_bots(rig, "bot-reminders-a1b2", "bot-researcher-b2c3",
                     "bot-notes-d4e5")
    registry.create_room("100", ["bot-reminders-a1b2", "bot-researcher-b2c3"])
    interaction = _FakeInteraction(channel_id="100", guild_id="42")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)

    _run(rig.adapter._concierge_cmd_room_add(interaction, "bot-notes-d4e5"))

    assert registry.room_members("100") == [
        "bot-reminders-a1b2", "bot-researcher-b2c3", "bot-notes-d4e5"]
    text = interaction.followup_log[-1][0]
    assert "bot-notes-d4e5" in text and "3/6" in text


def test_room_add_refusals(rig, monkeypatch):
    """Not-a-room refusal (actionable copy), unbound-bot refusal."""
    _bind_guild_bots(rig, "bot-reminders-a1b2", "bot-researcher-b2c3")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)

    # invoking channel is NOT a room
    interaction = _FakeInteraction(channel_id="777", guild_id="42")
    _run(rig.adapter._concierge_cmd_room_add(interaction, "bot-reminders-a1b2"))
    assert "isn't a bot room" in interaction.response.sent[-1][0]
    assert "`/room create`" in interaction.response.sent[-1][0]

    # a room, but the bot is not one of this guild's
    registry.create_room("100", ["bot-reminders-a1b2", "bot-researcher-b2c3"])
    interaction = _FakeInteraction(channel_id="100", guild_id="42")
    _run(rig.adapter._concierge_cmd_room_add(interaction, "bot-stranger-9999"))
    assert "isn't one of this server's bots" in interaction.response.sent[-1][0]
    assert registry.room_members("100") == [
        "bot-reminders-a1b2", "bot-researcher-b2c3"]


def test_room_add_surfaces_the_registry_cap(rig, monkeypatch):
    """The registry is the authority: its full-room ValueError surfaces
    cleanly as user copy (the UX validator and the registry are two halves
    of one bound)."""
    members = [f"bot-m{i}-aaaa" for i in range(6)]
    _bind_guild_bots(rig, *members)
    _bind_guild_bots(rig, "bot-extra-bbbb")
    registry.create_room("100", members)
    interaction = _FakeInteraction(channel_id="100", guild_id="42")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)

    _run(rig.adapter._concierge_cmd_room_add(interaction, "bot-extra-bbbb"))

    text = interaction.followup_log[-1][0]
    assert "full" in text or "6" in text
    assert len(registry.room_members("100")) == 6


def test_room_remove_round_trip_and_last_removal_disbands(rig, monkeypatch):
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)

    registry.create_room("100", ["bot-reminders-a1b2", "bot-researcher-b2c3"])
    interaction = _FakeInteraction(channel_id="100", guild_id="42")
    _run(rig.adapter._concierge_cmd_room_remove(interaction, "bot-researcher-b2c3"))
    assert registry.room_members("100") == ["bot-reminders-a1b2"]
    assert "Removed" in interaction.followup_log[-1][0]
    assert "1/6" in interaction.followup_log[-1][0]

    # the LAST removal disbands the room (registry contract) — surfaced
    interaction = _FakeInteraction(channel_id="100", guild_id="42")
    _run(rig.adapter._concierge_cmd_room_remove(interaction, "bot-reminders-a1b2"))
    assert registry.room_members("100") == []  # the room is gone
    assert "disbanded" in interaction.followup_log[-1][0]


def test_room_remove_refusals(rig, monkeypatch):
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)

    # not a room
    interaction = _FakeInteraction(channel_id="777", guild_id="42")
    _run(rig.adapter._concierge_cmd_room_remove(interaction, "bot-reminders-a1b2"))
    assert "isn't a bot room" in interaction.response.sent[-1][0]

    # a room, but the bot is not in it
    registry.create_room("100", ["bot-reminders-a1b2"])
    interaction = _FakeInteraction(channel_id="100", guild_id="42")
    _run(rig.adapter._concierge_cmd_room_remove(interaction, "bot-researcher-b2c3"))
    assert "isn't in this room" in interaction.response.sent[-1][0]
    assert registry.room_members("100") == ["bot-reminders-a1b2"]


def test_room_disband_confirmation_flow(rig, monkeypatch):
    """/room disband: confirmation button, then registry-driven teardown
    (every member removed — the last removal deletes the room key), with a
    confirming reply. Cancel leaves everything."""
    _bind_guild_bots(rig, "bot-reminders-a1b2", "bot-researcher-b2c3")
    registry.create_room("100", ["bot-reminders-a1b2", "bot-researcher-b2c3"])
    interaction = _FakeInteraction(channel_id="100", guild_id="42", user_id="u1")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)
    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)

    _run(rig.adapter._concierge_cmd_room_disband(interaction))

    _text, view, _eph = interaction.response.sent[-1]
    assert "Disband this room?" in _text
    assert "channel itself stays" in _text
    disband_btn, cancel_btn = view.children[0], view.children[1]

    # cancel first: nothing happens
    cancel_click = _FakeInteraction(channel_id="100", guild_id="42")
    _run(cancel_btn.callback(cancel_click))
    assert "Cancelled" in cancel_click.response.sent[-1][0]
    assert registry.room_members("100") == [
        "bot-reminders-a1b2", "bot-researcher-b2c3"]

    # then confirm: the room is gone, the reply confirms
    confirm_click = _FakeInteraction(channel_id="100", guild_id="42")
    _run(disband_btn.callback(confirm_click))
    assert registry.room_members("100") == []
    text = confirm_click.followup_log[-1][0]
    assert "disbanded" in text and "channel" in text


def test_room_disband_refuses_not_a_room(rig, monkeypatch):
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)
    interaction = _FakeInteraction(channel_id="777", guild_id="42")

    _run(rig.adapter._concierge_cmd_room_disband(interaction))

    assert "isn't a bot room" in interaction.response.sent[-1][0]


# ── M5c discipline for /room: defer before the mutation, work off the loop ──


def test_room_create_defers_before_the_registry_write(rig, monkeypatch):
    """ORDER contract (room create select): defer() precedes the
    create_room registry write; the outcome rides the followup branch."""
    order = []
    _bind_guild_bots(rig, "bot-reminders-a1b2", "bot-researcher-b2c3")
    real_create_room = registry.create_room

    def fake_create_room(channel_id, members):
        order.append("create_room")
        return real_create_room(channel_id, members)

    monkeypatch.setattr(registry, "create_room", fake_create_room)

    async def _ok_create(**kw):
        return "9999"

    monkeypatch.setattr(rig.adapter, "_concierge_create_channel", _ok_create)
    interaction = _FakeInteraction(channel_id="100", guild_id="42")
    view = _room_picker_from_command(rig, monkeypatch, interaction)
    pick = _FakeInteraction(
        channel_id="100", guild_id="42", order=order,
        data={"values": ["bot-reminders-a1b2", "bot-researcher-b2c3"]})
    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)

    _run(view.children[0].callback(pick))

    assert order.index("defer") < order.index("create_room")
    assert pick.followup_log
    assert pick.response.sent == []


def test_room_registry_writes_run_off_the_event_loop(rig, monkeypatch):
    """OFFLOAD contract: create_room / add_to_room / the disband teardown
    are flock-guarded sync writes — they run on worker threads, never the
    discord.py event-loop thread."""
    seen = {}
    _bind_guild_bots(rig, "bot-reminders-a1b2", "bot-researcher-b2c3",
                     "bot-notes-d4e5")
    real_create_room = registry.create_room
    real_add = registry.add_to_room
    real_remove = registry.remove_from_room

    def fake_create_room(channel_id, members):
        seen["create_room"] = threading.get_ident()
        return real_create_room(channel_id, members)

    def fake_add(channel_id, profile):
        seen["add_to_room"] = threading.get_ident()
        return real_add(channel_id, profile)

    def fake_remove(channel_id, profile):
        seen["remove_from_room"] = threading.get_ident()
        return real_remove(channel_id, profile)

    monkeypatch.setattr(registry, "create_room", fake_create_room)
    monkeypatch.setattr(registry, "add_to_room", fake_add)
    monkeypatch.setattr(registry, "remove_from_room", fake_remove)
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)
    monkeypatch.setattr(
        rig.adapter, "_concierge_component_auth", lambda i: True)

    async def _ok_create(**kw):
        return "9999"

    monkeypatch.setattr(rig.adapter, "_concierge_create_channel", _ok_create)

    # create
    interaction = _FakeInteraction(channel_id="100", guild_id="42")
    view = _room_picker_from_command(rig, monkeypatch, interaction)
    pick = _FakeInteraction(
        channel_id="100", guild_id="42",
        data={"values": ["bot-reminders-a1b2", "bot-researcher-b2c3"]})
    _run(view.children[0].callback(pick))

    # add
    add_interaction = _FakeInteraction(channel_id="9999", guild_id="42")
    _run(rig.adapter._concierge_cmd_room_add(add_interaction, "bot-notes-d4e5"))

    # remove
    remove_interaction = _FakeInteraction(channel_id="9999", guild_id="42")
    _run(rig.adapter._concierge_cmd_room_remove(
        remove_interaction, "bot-notes-d4e5"))

    # disband
    disband_interaction = _FakeInteraction(
        channel_id="9999", guild_id="42", user_id="u1")
    _run(rig.adapter._concierge_cmd_room_disband(disband_interaction))
    _text, view, _eph = disband_interaction.response.sent[-1]
    confirm = _FakeInteraction(channel_id="9999", guild_id="42")
    _run(view.children[0].callback(confirm))

    for name in ("create_room", "add_to_room", "remove_from_room"):
        assert seen[name] != threading.get_ident(), name
    assert registry.room_members("9999") == []  # the round-trip held


# ── Task 8.2: /connect — account-connection deep links ─────────────────────


def _bind_for_connect(rig, channel_id, profile, blueprint,
                       guild_id="42"):
    registry.bind_channel(channel_id, profile=profile, blueprint=blueprint,
                          guild_id=guild_id)


def test_connect_no_arg_lists_kinds_with_status_and_buttons(
        rig, monkeypatch):
    """/connect with no kind: every account kind this guild's bound bots
    need, with configured/unconfigured status, and a deep-link button per
    UNCONFIGURED kind (built from the dashboard base URL + the discovered
    page path)."""
    from concierge import connections

    monkeypatch.setattr(
        connections, "dashboard_base_url",
        lambda: "https://gw.example.com")
    monkeypatch.setattr(
        connections, "account_probe",
        lambda profile: (lambda kind: kind == "email"))
    # scheduler (calendar) + email_triage (email) in this guild
    _bind_for_connect(rig, "100", "bot-reminders-a1b2", "scheduler")
    _bind_for_connect(rig, "101", "bot-researcher-b2c3", "email_triage")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)

    interaction = _FakeInteraction()
    _run(rig.adapter._concierge_cmd_connect(interaction, None))

    text, view, ephemeral = interaction.response.sent[0]
    assert ephemeral is True
    assert "Calendar" in text and "not connected" in text
    assert "Email" in text and "connected" in text
    # exactly the unconfigured kind got a deep-link button
    buttons = [c for c in view.children if getattr(c, "url", None)]
    assert [b.url for b in buttons] == [
        "https://gw.example.com/settings?tab=keys"]
    assert "Calendar" in buttons[0].label


def test_connect_no_arg_without_dashboard_base_skips_buttons(
        rig, monkeypatch):
    """No dashboard origin configured: the listing still answers with
    honest status, no dead buttons."""
    from concierge import connections

    monkeypatch.setattr(connections, "dashboard_base_url", lambda: "")
    monkeypatch.setattr(
        connections, "account_probe",
        lambda profile: (lambda kind: False))
    _bind_for_connect(rig, "100", "bot-reminders-a1b2", "scheduler")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)

    interaction = _FakeInteraction()
    _run(rig.adapter._concierge_cmd_connect(interaction, None))

    text, view, _ = interaction.response.sent[0]
    assert "not connected" in text
    assert view is None or [c for c in view.children
                            if getattr(c, "url", None)] == []


def test_connect_no_arg_with_no_bots_is_actionable(rig, monkeypatch):
    from concierge import connections

    monkeypatch.setattr(
        connections, "dashboard_base_url", lambda: "https://gw.example.com")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)

    interaction = _FakeInteraction()
    _run(rig.adapter._concierge_cmd_connect(interaction, None))

    text, _, _ = interaction.response.sent[0]
    assert "No bots" in text


def test_connect_kind_arg_replies_with_the_deep_link(rig, monkeypatch):
    """/connect email: an ephemeral reply carrying the deep-link button for
    that kind (built from the same pure payload builder as the nudge)."""
    from concierge import connections

    monkeypatch.setattr(
        connections, "dashboard_base_url",
        lambda: "https://gw.example.com")
    monkeypatch.setattr(
        connections, "account_probe",
        lambda profile: (lambda kind: False))
    _bind_for_connect(rig, "100", "bot-reminders-a1b2", "email_triage")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)

    interaction = _FakeInteraction()
    _run(rig.adapter._concierge_cmd_connect(interaction, "email"))

    text, view, ephemeral = interaction.response.sent[0]
    assert ephemeral is True
    assert "Email" in text
    buttons = [c for c in view.children if getattr(c, "url", None)]
    assert [b.url for b in buttons] == ["https://gw.example.com/messaging"]


def test_connect_kind_arg_unknown_kind_is_an_actionable_refusal(
        rig, monkeypatch):
    from concierge import connections

    monkeypatch.setattr(
        connections, "dashboard_base_url",
        lambda: "https://gw.example.com")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)

    interaction = _FakeInteraction()
    _run(rig.adapter._concierge_cmd_connect(interaction, "weather"))

    text, view, _ = interaction.response.sent[0]
    assert "weather" in text
    # the refusal names what CAN be linked — email and calendar are real
    assert "calendar" in text and "email" in text
    assert view is None


def test_connect_kind_arg_configured_is_a_clean_nothing_to_do(
        rig, monkeypatch):
    from concierge import connections

    monkeypatch.setattr(
        connections, "dashboard_base_url",
        lambda: "https://gw.example.com")
    monkeypatch.setattr(
        connections, "account_probe",
        lambda profile: (lambda kind: True))
    _bind_for_connect(rig, "100", "bot-reminders-a1b2", "scheduler")
    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _allow)

    interaction = _FakeInteraction()
    _run(rig.adapter._concierge_cmd_connect(interaction, "calendar"))

    text, view, _ = interaction.response.sent[0]
    assert "connected" in text
    assert view is None or [c for c in view.children
                           if getattr(c, "url", None)] == []


def test_connect_is_gated_like_bot_and_room(rig, monkeypatch):
    """Same self-gating as the other concierge commands: a gate refusal
    answers nothing (the adapter's own gate copy handles the user)."""
    async def _refuse(*_a, **_kw):
        return False

    monkeypatch.setattr(
        rig.adapter, "_concierge_gate_interaction", _refuse)
    interaction = _FakeInteraction()
    _run(rig.adapter._concierge_cmd_connect(interaction, None))
    assert interaction.response.sent == []


# ---------------------------------------------------------------------------
# Real component-auth gate (regression: the 2026-09-27 live-deny bug)
# ---------------------------------------------------------------------------

class TestRealComponentAuthGate:
    """The picker/confirm auth must work on the REAL adapter shape.

    Live bug (2026-09-27 E2E): ``_component_check_auth`` is a MODULE function
    on the adapter, never an instance attribute/method, so the mixin's
    ``getattr(self, "_component_check_auth", None)`` resolved to None and the
    gate returned False for EVERY user — every picker click got "You're not
    allowed to use this." The handler tests above stub
    ``_concierge_component_auth``, so they never exercised the real delegate;
    these tests pin the real-class behavior without the stub.
    """

    def _rig_adapter(self, tmp_path, monkeypatch, allowed):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = object.__new__(DiscordAdapter)
        adapter.gateway_runner = None
        adapter._allowed_user_ids = set(allowed)
        adapter._allowed_role_ids = set()
        return adapter

    def _interaction_for(self, uid):
        return SimpleNamespace(user=SimpleNamespace(id=uid, roles=None))

    def test_allowed_user_passes_the_real_gate(self, tmp_path, monkeypatch):
        adapter = self._rig_adapter(tmp_path, monkeypatch, ["1551304278537150584"])
        assert adapter._concierge_component_auth(
            self._interaction_for(1551304278537150584)) is True

    def test_stranger_is_denied_by_the_real_gate(self, tmp_path, monkeypatch):
        adapter = self._rig_adapter(tmp_path, monkeypatch, ["1551304278537150584"])
        assert adapter._concierge_component_auth(
            self._interaction_for(1111111111111111111)) is False

    def test_allow_all_env_passes_the_real_gate(self, tmp_path, monkeypatch):
        from plugins.platforms.discord import adapter_component_auth as component_auth_mod
        adapter = self._rig_adapter(tmp_path, monkeypatch, [])
        # The approval-card refactor moved _component_check_auth (and its scoped
        # env reads) out of adapter.py into adapter_component_auth — patch the
        # binding the gate actually reads.
        monkeypatch.setattr(
            component_auth_mod, "_scoped_gate_env",
            lambda key, default="": "true" if key == "DISCORD_ALLOW_ALL_USERS" else default)
        assert adapter._concierge_component_auth(
            self._interaction_for(2222222222222222222)) is True
