"""Card H — ``/bot category`` (private) and ``/bot space`` (public) admin surface.

Pins the HOME_CATEGORY_DESIGN §3 / §12.5 contract without live discord.py
plumbing: the mixin's slash handlers are driven against fake guild/interaction
objects and monkeypatched registry/provision services, so these tests are
self-contained (no dependency on the sibling registry/config/routing cards).

Contracts covered:
- /bot category <user> creates a PRIVATE category named ``<display-name>``
  (the old ``-private`` suffix is gone), with @everyone denied and the target
  member + guild admins granted view/manage;
- /bot space <name> creates a PUBLIC category named the operator topic, with
  NO default-role overwrite;
- both then create the general channel, provision the orchestrator only when
  the user has none, record home_category + home_channel + guild_id, and write
  bind_channel LAST;
- the created channel gets its auto-offer suppressed;
- the ephemeral reply carries BOTH the category id and the general channel id;
- re-running for a user with a resolving home_category ADOPTS instead of
  duplicating;
- both stay admin-only via concierge/auth_tiers.is_admin (short refusal).
"""

import asyncio
import socket
import sys
import types
from types import SimpleNamespace

import pytest

from tests.gateway.conftest import _ensure_discord_mock

_ensure_discord_mock()

import discord  # the conftest mock, installed above

from concierge import provision, registry


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── the rig (mirrors test_discord_concierge_commands.rig) ──────────────────


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from gateway.platforms.base import BasePlatformAdapter

    root = tmp_path
    (root / "concierge").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))

    # gateway.run imports hermes_bootstrap at module level; under a live install
    # its PM activation stats the real ~/.hermes/manifest.json and trips
    # tests/home_io_guard. Stub the one symbol the import chain pulls.
    _bootstrap = types.ModuleType("hermes_bootstrap")
    _bootstrap._happy_eyeballs_create_connection = socket.create_connection
    sys.modules.setdefault("hermes_bootstrap", _bootstrap)

    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.run import GatewayRunner
    from plugins.platforms.discord.adapter import DiscordAdapter

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.platforms = {
        Platform.DISCORD: PlatformConfig(enabled=True, extra={})
    }
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


# ── fakes ──────────────────────────────────────────────────────────────────


class _Obj:
    """Hashable stand-in for a discord Member/Role (SimpleNamespace is not
    hashable, so it cannot be a PermissionOverwrite dict key)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _PO:
    """Capturable stand-in for discord.PermissionOverwrite."""

    def __init__(self, **kw):
        self.kw = kw


class _FakeGuild:
    def __init__(self, members=()):
        self.id = 42
        self.default_role = "@everyone"
        self.members = list(members)
        self.by_id = {m.id: m for m in self.members}
        self.created = []
        self.channels = {}
        # The bot's own member. It must land in the category overwrites or the bot
        # cannot see or post in the space it just created (403 Missing Access,
        # observed live 2026-11-15). Deliberately NOT an administrator.
        self.me = _Obj(id=7, name="Hermes-Concierge",
                       guild_permissions=_Obj(administrator=False))

    def get_member(self, uid):
        return self.by_id.get(uid)

    async def fetch_member(self, uid):
        return self.by_id.get(uid)

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def create_category(self, *, name, overwrites=None):
        cat = SimpleNamespace(id=9001, name=name)
        self.created.append((name, overwrites))
        self.channels[9001] = cat
        return cat


class _Resp:
    def __init__(self):
        self.deferred = False
        self.sent = []

    def is_done(self):
        return self.deferred or bool(self.sent)

    async def send_message(self, text, *, view=None, ephemeral=False):
        self.sent.append((text, ephemeral))

    async def defer(self, *, ephemeral=False, **_kw):
        self.deferred = True


class _Followup:
    def __init__(self, log):
        self.log = log

    async def send(self, text, *, view=None, ephemeral=False):
        self.log.append((text, ephemeral))


class _FakeInteraction:
    def __init__(self, guild, *, user_id="admin1"):
        self.response = _Resp()
        self.followup_log = []
        self.followup = _Followup(self.followup_log)
        self.guild = guild
        self.user = _Obj(
            id=user_id, name="Admin", display_name="Admin")

    def all_replies(self):
        return self.response.sent + self.followup_log


# ── service harness ────────────────────────────────────────────────────────


def _install(rig, monkeypatch, *, users=None):
    """Monkeypatch registry + provision + channel-create; return (log, store)."""
    store = {"users": dict(users or {})}
    log = []

    def get_user(uid):
        entry = store["users"].get(str(uid))
        return dict(entry) if entry else None

    def home_category_for_user(uid):
        entry = store["users"].get(str(uid))
        return entry.get("home_category") if entry else None

    def home_channel_for_user(uid):
        entry = store["users"].get(str(uid))
        return entry.get("home_channel") if entry else None

    def orchestrator_for_user(uid):
        entry = store["users"].get(str(uid))
        return entry.get("orchestrator") if entry else None

    def update_user(uid, **fields):
        log.append(("update_user", str(uid), dict(fields)))
        entry = store["users"].setdefault(str(uid), {})
        entry.update(fields)
        return dict(entry)

    def bind_channel(cid, **kw):
        log.append(("bind_channel", str(cid), dict(kw)))
        return dict(kw)

    def create_bot(blueprint_key, **kw):
        log.append(("create_bot", blueprint_key, dict(kw)))
        return {"profile": "bot-orchestrator-xyz",
                "orchestrator": "bot-orchestrator-xyz"}

    monkeypatch.setattr(registry, "get_user", get_user)
    monkeypatch.setattr(
        registry, "home_category_for_user", home_category_for_user,
        raising=False)
    monkeypatch.setattr(registry, "home_channel_for_user", home_channel_for_user)
    monkeypatch.setattr(registry, "orchestrator_for_user", orchestrator_for_user)
    monkeypatch.setattr(registry, "update_user", update_user)
    monkeypatch.setattr(registry, "bind_channel", bind_channel)
    monkeypatch.setattr(provision, "create_bot", create_bot)
    # The mixin does ``import discord`` INSIDE the method, i.e. it reads
    # sys.modules at call time. Patch THAT object (a sibling test file may have
    # reinstalled the mock after this module was imported), plus our own ref.
    _discord_mod = sys.modules.get("discord", discord)
    monkeypatch.setattr(
        _discord_mod, "PermissionOverwrite", _PO, raising=False)
    monkeypatch.setattr(discord, "PermissionOverwrite", _PO, raising=False)

    async def _create_channel(*, guild_id, category_id, name):
        log.append(("create_channel", guild_id, str(category_id), name))
        # Mirror the REAL _concierge_create_channel (which Card F's shared
        # _concierge_ensure_general_channel rides): mark the channel so the
        # channel-create auto-offer is suppressed. Card H no longer re-asserts
        # the suppression itself — it is the create helper's job.
        rig.adapter._concierge_remember_hub_created_channel("9100")
        return "9100"

    monkeypatch.setattr(
        rig.adapter, "_concierge_create_channel", _create_channel)
    return log, store


def _rows(log, name):
    """All log rows of one kind (name), in emission order."""
    return [row for row in log if row[0] == name]


def _wire_client(rig, guild):
    """Card F's shared helpers resolve the guild / category liveness through
    the hub's OWN client (self._client), not interaction.guild. Wire the fake
    guild in so _concierge_ensure_home_category can create the category."""
    rig.adapter._client = SimpleNamespace(
        get_guild=lambda _gid: guild,
        guilds=[guild],
        get_channel=lambda cid: guild.get_channel(cid),
    )


def _names(log):
    return [row[0] for row in log]


# ── /bot category: PRIVATE ─────────────────────────────────────────────────


def test_category_private_full_flow(rig, monkeypatch):
    log, _ = _install(rig, monkeypatch)

    admin = _Obj(
        id=1, display_name="Root",
        guild_permissions=SimpleNamespace(administrator=True))
    target = _Obj(id=500, display_name="Alice")
    guild = _FakeGuild(members=[admin, target])
    _wire_client(rig, guild)          # Card F helper resolves guild via _client
    inter = _FakeInteraction(guild)

    _run(rig.adapter._concierge_cmd_category(inter, "<@500>"))

    # category name is the display name — the -private suffix is gone
    assert len(guild.created) == 1
    name, overwrites = guild.created[0]
    assert name == "Alice"
    assert "-private" not in name
    # private: @everyone denied; target + admins granted view + manage
    assert guild.default_role in overwrites
    assert overwrites[guild.default_role].kw == {"view_channel": False}
    assert target in overwrites
    assert overwrites[target].kw == {"view_channel": True,
                                     "manage_channels": True}
    assert admin in overwrites
    assert overwrites[admin].kw == {"view_channel": True,
                                    "manage_channels": True}
    # the bot itself: without this it 403s "Missing Access" on its own channel
    assert guild.me in overwrites
    assert overwrites[guild.me].kw == {"view_channel": True,
                                       "send_messages": True,
                                       "manage_channels": True}

    # Step order: the shared category helper records home_category
    # (best-effort) FIRST, then the general channel, provision, H's
    # authoritative triple record, and bind_channel LAST.
    assert _names(log) == [
        "update_user", "create_channel", "create_bot", "update_user",
        "bind_channel"]
    ups = _rows(log, "update_user")
    assert ups[0][2] == {"home_category": "9001"}   # Card F helper's write
    cc = _rows(log, "create_channel")[0]
    assert cc[1] == "42" and cc[2] == "9001" and cc[3] == "general"
    cb = _rows(log, "create_bot")[0]
    assert cb[1] == "orchestrator"
    assert cb[2]["user_id"] == "500" and cb[2]["channel_id"] == "9100"
    assert ups[-1][2] == {"home_category": "9001", "home_channel": "9100",
                          "guild_id": "42"}         # H's authoritative record
    bind = _rows(log, "bind_channel")[0]
    assert bind[1] == "9100"
    assert bind[2]["profile"] == "bot-orchestrator-xyz"
    assert bind[2]["blueprint"] == "orchestrator"
    assert _names(log)[-1] == "bind_channel"         # nothing routes until then

    # auto-offer suppressed on the created channel
    assert rig.adapter._concierge_channel_is_hub_created("9100")

    # The command answers TWICE: once as soon as the category + channel exist (so the
    # operator is never left waiting out Discord's 15-minute deferred window while
    # provisioning runs), then the completion line. BOTH carry the two ids.
    replies = inter.all_replies()
    assert len(replies) == 2
    for text, ephemeral in replies:
        assert ephemeral is True
        assert "9001" in text and "9100" in text
    # the first is the progress note, the last is the completion
    assert "Provisioning" in replies[0][0]
    assert "Provisioning" not in replies[-1][0]


def test_category_reuses_existing_orchestrator(rig, monkeypatch):
    """Provision is skipped when the user already owns an orchestrator."""
    log, _ = _install(rig, monkeypatch, users={
        "500": {"orchestrator": "bot-orchestrator-existing"},
    })
    guild = _FakeGuild()
    _wire_client(rig, guild)
    inter = _FakeInteraction(guild)

    _run(rig.adapter._concierge_cmd_category(inter, "500"))

    assert "create_bot" not in _names(log)
    assert _names(log)[-1] == "bind_channel"
    assert log[-1][2]["profile"] == "bot-orchestrator-existing"


# ── /bot space: PUBLIC ─────────────────────────────────────────────────────


def test_space_public_full_flow(rig, monkeypatch):
    log, _ = _install(rig, monkeypatch)
    # The shared helper resolves the member by NUMERIC id (real Discord ids
    # are numeric), so register the caller as a guild member and make it the
    # interaction user (the same object the overwrite dict is keyed by).
    caller = _Obj(id=700, display_name="Admin", name="Admin",
                  guild_permissions=SimpleNamespace(administrator=False))
    guild = _FakeGuild(members=[caller])
    _wire_client(rig, guild)
    inter = _FakeInteraction(guild, user_id="700")
    inter.user = caller

    _run(rig.adapter._concierge_cmd_space(inter, "design-reviews"))

    assert len(guild.created) == 1
    name, overwrites = guild.created[0]
    assert name == "design-reviews"          # operator topic, not a user name
    assert guild.default_role not in overwrites  # NO default-role overwrite
    # the caller keeps manage_channels
    assert caller in overwrites
    assert overwrites[caller].kw == {"manage_channels": True}
    # public still grants the bot explicitly rather than relying on the default role
    assert guild.me in overwrites
    assert overwrites[guild.me].kw == {"view_channel": True,
                                       "send_messages": True,
                                       "manage_channels": True}

    assert _names(log) == [
        "update_user", "create_channel", "create_bot", "update_user",
        "bind_channel"]
    assert _rows(log, "create_channel")[0][3] == "general"
    assert _rows(log, "create_bot")[0][2]["user_id"] == "700"
    assert _rows(log, "update_user")[-1][2] == {
        "home_category": "9001", "home_channel": "9100", "guild_id": "42"}
    assert _rows(log, "bind_channel")[0][2]["blueprint"] == "orchestrator"

    text, ephemeral = inter.all_replies()[0]
    assert ephemeral is True
    assert "9001" in text and "9100" in text


# ── idempotency: adopt, never duplicate ────────────────────────────────────


def test_category_adopts_existing_resolving_category(rig, monkeypatch):
    log, _ = _install(rig, monkeypatch, users={
        "500": {"home_category": "7777", "home_channel": "7888",
                "orchestrator": "bot-orchestrator-existing"},
    })
    guild = _FakeGuild()
    guild.channels[7777] = SimpleNamespace(id=7777, name="Alice")
    inter = _FakeInteraction(guild)

    _run(rig.adapter._concierge_cmd_category(inter, "500"))

    assert guild.created == []              # no duplicate category
    assert _names(log) == []                # no downstream writes at all
    text, ephemeral = inter.all_replies()[0]
    assert ephemeral is True
    assert "7777" in text and "7888" in text   # both ids reported


def test_space_adopts_existing_resolving_category(rig, monkeypatch):
    log, _ = _install(rig, monkeypatch, users={
        "admin1": {"home_category": "7777", "home_channel": "7888"},
    })
    guild = _FakeGuild()
    guild.channels[7777] = SimpleNamespace(id=7777, name="old-topic")
    inter = _FakeInteraction(guild, user_id="admin1")

    _run(rig.adapter._concierge_cmd_space(inter, "new-topic"))

    assert guild.created == []
    assert _names(log) == []


def test_stale_category_is_recreated(rig, monkeypatch):
    """A recorded home_category that no longer resolves is recreated."""
    log, _ = _install(rig, monkeypatch, users={
        "500": {"home_category": "7777", "home_channel": "7888"},
    })
    guild = _FakeGuild()                    # 7777 absent -> not resolving
    _wire_client(rig, guild)
    inter = _FakeInteraction(guild)

    _run(rig.adapter._concierge_cmd_category(inter, "500"))

    assert len(guild.created) == 1
    assert _names(log)[-1] == "bind_channel"


# ── real-registry end-to-end (card B's home_category field is live) ─────────


def test_real_registry_persists_triple_binds_and_adopts(rig, monkeypatch):
    """Against the REAL registry (no monkeypatched registry functions): the
    home_category/home_channel/guild_id triple is persisted, the general
    channel is truly bound, and a second run ADOPTS (no duplicate)."""
    import json

    def create_bot(bk, **kw):
        # Mirror provision's users-section bookkeeping: real create_bot calls
        # set_user here, which gives update_user an entry to merge into.
        registry.set_user(
            kw["user_id"], orchestrator="bot-orchestrator-xyz",
            guild_id=kw.get("guild_id"))
        return {"profile": "bot-orchestrator-xyz"}

    monkeypatch.setattr(provision, "create_bot", create_bot)
    _discord_mod = sys.modules.get("discord", discord)
    monkeypatch.setattr(
        _discord_mod, "PermissionOverwrite", _PO, raising=False)
    monkeypatch.setattr(discord, "PermissionOverwrite", _PO, raising=False)

    async def _create_channel(*, guild_id, category_id, name):
        return "9100"

    monkeypatch.setattr(
        rig.adapter, "_concierge_create_channel", _create_channel)

    guild = _FakeGuild()
    _wire_client(rig, guild)
    inter = _FakeInteraction(guild)
    _run(rig.adapter._concierge_cmd_category(inter, "500"))

    raw = json.loads((rig.root / "concierge" / "registry.json").read_text())
    entry = raw["users"]["500"]
    assert entry["home_category"] == "9001"
    assert entry["home_channel"] == "9100"
    assert entry["guild_id"] == "42"
    assert raw["bindings"]["9100"]["profile"] == "bot-orchestrator-xyz"
    assert raw["bindings"]["9100"]["blueprint"] == "orchestrator"

    assert len(guild.created) == 1
    inter2 = _FakeInteraction(guild)
    _run(rig.adapter._concierge_cmd_category(inter2, "500"))
    assert len(guild.created) == 1          # adopted, not duplicated
    assert any("Adopted" in t for t, _ in inter2.all_replies())


# ── ONE naming authority ----------------------------------------------------


def test_general_channel_name_single_authority(rig, monkeypatch):
    """Exactly ONE function decides the general-channel name. The DEFAULT
    (bootstrap / no-home-category fallback) is byte-for-byte
    ``orchestrator-<slug>`` — invariant 3, zero behavior change when
    ``home_category.enabled: false``. ``home_space=True`` (the admin surface)
    uses the card-A config-driven name (default ``general``)."""
    from concierge import settings

    fn = rig.adapter._concierge_general_channel_name
    src = SimpleNamespace(user_name="Alice", user_id="500")

    # default + explicit False both FREEZE the bootstrap/legacy name
    assert fn(src) == "orchestrator-alice"
    assert fn(src, home_space=False) == "orchestrator-alice"
    # slug falls back to the user id, then to a never-empty slug
    assert fn(SimpleNamespace(user_name=None, user_id="42")) == \
        "orchestrator-42"

    # the two OLD naming functions are gone: no caller may bypass the seam
    assert not hasattr(rig.adapter, "_concierge_orchestrator_channel_name")
    assert not hasattr(rig.adapter, "_concierge_home_general_channel_name")

    # home_space=True -> the config-driven name (default "general")
    assert fn(src, home_space=True) == "general"

    # ...and it HONORS concierge.home_category.general_channel when set
    monkeypatch.setattr(
        settings, "concierge_settings",
        lambda: SimpleNamespace(
            home_category=SimpleNamespace(general_channel="lobby")))
    assert fn(src, home_space=True) == "lobby"
    # the fallback lane NEVER consults the config (invariant 3)
    assert fn(src, home_space=False) == "orchestrator-alice"


# ── admin-only gate (auth_tiers, no duplicated mechanism) ───────────────────


def _slash_commands(rig):
    class _Tree:
        def __init__(self):
            self.commands = []

        def add_command(self, cmd):
            self.commands.append(cmd)

    tree = _Tree()
    rig.adapter._concierge_register_slash(tree)
    group = next(c for c in tree.commands if c.name == "bot")
    return group._children


def test_non_admin_refused_on_both_commands(rig, monkeypatch):
    monkeypatch.setattr("concierge.auth_tiers.is_admin", lambda uid: False)
    called = []

    async def _space(i, n):
        called.append("space")

    async def _category(i, u):
        called.append("category")

    monkeypatch.setattr(rig.adapter, "_concierge_cmd_space", _space)
    monkeypatch.setattr(rig.adapter, "_concierge_cmd_category", _category)
    cmds = _slash_commands(rig)

    for cmd_name, arg in (("category", "500"), ("space", "topic")):
        inter = _FakeInteraction(_FakeGuild())
        _run(cmds[cmd_name].callback(inter, arg))
        assert any("admin-only" in t for t, _ in inter.all_replies())
    assert called == []                     # handlers never reached


def test_admin_allowed_on_both_commands(rig, monkeypatch):
    monkeypatch.setattr("concierge.auth_tiers.is_admin", lambda uid: True)
    called = []

    async def _space(i, n):
        called.append("space")

    async def _category(i, u):
        called.append("category")

    monkeypatch.setattr(rig.adapter, "_concierge_cmd_space", _space)
    monkeypatch.setattr(rig.adapter, "_concierge_cmd_category", _category)
    cmds = _slash_commands(rig)

    _run(cmds["category"].callback(_FakeInteraction(_FakeGuild()), "500"))
    _run(cmds["space"].callback(_FakeInteraction(_FakeGuild()), "topic"))
    assert called == ["category", "space"]
