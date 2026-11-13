"""Concierge orchestrator redesign, T4 (design §4): the unbound→orchestrator
fallback, the first-contact bootstrap, and admission branch (c).

Three seams, one contract per seam:

- STAMPING, at the registry-verdict tail (``_concierge_apply_registry_verdict``,
  the concierge_voice sibling): an UNBOUND channel's author with a LIVE
  orchestrator gets the process-local ``_CONCIERGE_ORCHESTRATOR_ATTR`` stamp —
  never a re-home (the category check needs the RAW channel, which lives on
  the event, not the source). Bot-authored sources never get the stamp
  (anti-loop), and dead/uncheckable orchestrators warn once and stay unstamped
  (the ``_profile_exists`` posture).
- CONSUMPTION, at the runner-seam wrap (``_concierge_inbound_dispatch``): a
  human-authored, managed-category event carrying the stamp re-homes
  ``source.profile`` to the orchestrator before delegation (admission confirms
  via branch (c)); a human-authored, managed-category, UNBOUND event from an
  author with NO live orchestrator spawns ONE background bootstrap
  (provision → personal channel → binding → notices), never blocking the
  triggering message (the hub answers it; the NEXT message routes to the
  orchestrator).
- ADMISSION, branch (c) of the duck-typed resolver
  (``_concierge_dynamic_runtime_profile``): the users section admits a stamp
  that equals the author's registry orchestrator — the registry is the routing
  source of truth, and core still never imports the plugin layer.

Fail-closed everywhere: outside/unresolvable categories, bot authors, bound
channels, bootstrap failures — today's hub behavior, unchanged.

Rig: mirrors tests/gateway/test_discord_concierge_routing.py (adapter + temp
HERMES_HOME registry) and test_discord_concierge_runtime_admission.py (the
pure resolver tests). Real imports against a temp home; the slow externals
(provision.create_bot) are faked at the module the lazy import resolves.
"""

import asyncio
import io
import logging
import socket
import sys
import threading
import types
from types import SimpleNamespace

import pytest
from ruamel.yaml import YAML

from concierge import provision, registry
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.profile_routing import parse_profile_routes
from gateway.platforms.base import BasePlatformAdapter

from tests.gateway.conftest import _ensure_discord_mock

_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter
from plugins.platforms.discord import concierge_mixin, concierge_voice
from plugins.platforms.discord.concierge_mixin import ConciergeDiscordMixin

ORCH = "bot-orchestrator-a1b2"     # the author's (fake-)provisioned orchestrator
DEAD_ORCH = "bot-orchestrator-zz9"  # a registry entry whose profile dir is gone
BOUND_BOT = "bot-reminders-a1b2"   # a channel-bound bot, for the bound-channel rows
MANAGED_CATEGORY = "31337"         # the guild's managed category
UNMANAGED_CATEGORY = "999"         # a category the concierge does not manage
HOME_CHANNEL = "555"               # the bootstrap's personal channel id

_RUNNER_HOLDER: dict = {}


# ── The rig (mirrors test_discord_concierge_routing.py) ─────────────────────


@pytest.fixture
def concierge_rig_base(tmp_path, monkeypatch):
    """Multiplexed hub gateway with concierge on, plus live bot profile dirs.

    HERMES_HOME is a custom root (not ``<home>/.hermes``-shaped), so
    ``get_default_hermes_root`` takes the custom-root branch and ``<root>/
    profiles/`` is the directory ``profiles_to_serve`` reads — the served set a
    real hub gateway multiplexes over.
    """
    root = tmp_path
    for name in (ORCH, BOUND_BOT):
        (root / "profiles" / name).mkdir(parents=True)
        (root / "profiles" / name / ".env").write_text("OPENAI_API_KEY=sk-x\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    (root / "concierge").mkdir(exist_ok=True)

    # Live-install location artifact: gateway/run.py imports hermes_bootstrap,
    # whose MODULE-LEVEL PM activation stats the REAL ~/.hermes/manifest.json
    # when the checkout lives inside the home — stub it BEFORE the import
    # (same shape gateway/run.py already tolerates; see the routing suite).
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

    return SimpleNamespace(root=root, runner=runner, adapter=adapter)


@pytest.fixture
def concierge_rig(concierge_rig_base, monkeypatch):
    """The per-test rig, with the runner registered so the shared
    ``_set_concierge_config`` helper can stamp THIS test's in-hand section."""
    monkeypatch.setitem(_RUNNER_HOLDER, "runner", concierge_rig_base.runner)
    return concierge_rig_base


def _set_concierge_config(root, *, enabled: bool,
                          categories=(MANAGED_CATEGORY,)) -> None:
    """Write the hub's config.yaml AND stamp the runner's in-hand section (the
    config.yaml→GatewayConfig bridge) — the adapter seam reads the section from
    ``gateway_runner.config``, and the managed-category list rides the same
    section (``concierge_settings`` merges it over DEFAULT_CONFIG)."""
    buf = io.StringIO()
    YAML(typ="safe").dump(
        {"concierge": {"enabled": enabled,
                       "managed_category_ids": list(categories)}}, buf)
    (root / "config.yaml").write_text(buf.getvalue(), encoding="utf-8")
    runner = _RUNNER_HOLDER.get("runner")
    if runner is not None:
        runner.config.concierge = {"enabled": enabled,
                                   "managed_category_ids": list(categories)}


def _source(adapter, chat_id="100", **kw):
    kw.setdefault("chat_type", "group")
    kw.setdefault("user_id", "u1")
    return adapter.build_source(chat_id=str(chat_id), **kw)


class _RecordingChannel:
    """A duck-typed Discord channel capturing sends — a PLAIN marker class (a
    patched ``discord.Thread`` would make stubs Thread-instances, which the
    thread exemptions would then bypass; the rig traps, not re-enacted)."""

    def __init__(self, *, category_id=MANAGED_CATEGORY, channel_id=100):
        self.category_id = category_id
        self.id = channel_id
        self.sent = []

    async def send(self, text, **kw):
        self.sent.append(text)


def _orch_event(adapter, chat_id="100", channel=None, raw_id=123, **kw):
    """A MessageEvent whose source came from the REAL build_source — the
    orchestrator stamp lands exactly as production stamps it — entering the
    same runner-seam wrap (``_concierge_inbound_dispatch``) the live dispatch
    path funnels through. A guild message by default (managed categories are
    a guild concept)."""
    from gateway.platforms.event import MessageEvent

    kw.setdefault("guild_id", "42")
    source = _source(adapter, chat_id=chat_id, **kw)
    raw = (SimpleNamespace(channel=channel, id=raw_id)
           if channel is not None else None)
    return MessageEvent(text="hello", source=source, raw_message=raw)


def _install_fake_turn_spawner(monkeypatch, adapter, order):
    """Replace the delegation lane with a recorder: the wrap's 1:1 delegate
    (the live lane's runner handler) records the profile each dispatched
    event CARRIED — the mutated-profile contract the fallback re-home must
    satisfy. Everything above it (the wrap itself) runs REAL."""
    async def _handler(event):
        order.append(("dispatch", adapter._event_session_key(event),
                      getattr(event.source, "profile", None)))

    adapter.set_message_handler(_handler)
    return order


def _run_dispatch(adapter, event):
    """Drive the runner-seam wrap to completion on one fresh loop: await the
    background task(s) the dispatch spawned (a room fan-out driver or an
    orchestrator bootstrap — both register in the per-channel task map) so
    spawned work never outlives the scenario."""
    async def _scenario():
        await adapter._concierge_inbound_dispatch(event)
        task = getattr(adapter, "_concierge_room_fanout_tasks", {}).get(
            str(event.source.chat_id))
        if task is not None:
            await task
        for _key, t in list(getattr(adapter, "_session_tasks", {}).items()):
            if not t.done():
                await t

    asyncio.new_event_loop().run_until_complete(_scenario())


def _give_user_orchestrator(root, user_id="u1", profile=ORCH):
    """A users-section orchestrator entry the way T3's provision writes it
    (``registry.set_user``), over a LIVE profile dir — the fallback's
    liveness posture needs the dir to exist."""
    (root / "profiles" / profile).mkdir(parents=True, exist_ok=True)
    (root / "profiles" / profile / ".env").write_text("K=1\n")
    registry.set_user(user_id, orchestrator=profile, guild_id="42")


def _recording_provision(monkeypatch, rig, *, fail=False, block_on=None):
    """Fake ``provision.create_bot`` for the orchestrator mode: records the
    call, writes the users entry + profile dir the way the real one does
    (T3's own tests cover the real provision), and returns the entry. The
    real bootstrap consumes the return's ``orchestrator`` field."""
    calls = []

    def _fake_create_bot(blueprint_key, *, channel_id=None, user_id=None,
                        guild_id=None, **kw):
        calls.append({"blueprint": blueprint_key, "channel_id": channel_id,
                      "user_id": user_id, "guild_id": guild_id})
        if fail:
            raise RuntimeError("simulated provision failure")
        if block_on is not None:
            block_on.wait()  # a to_thread worker: sync wait is the test's gate
        _give_user_orchestrator(rig.root, user_id=str(user_id), profile=ORCH)
        return registry.get_user(str(user_id))

    monkeypatch.setattr(provision, "create_bot", _fake_create_bot)
    return calls


def _fake_discord_client(rig, *, home_channel_stub=None):
    """A duck-typed hub client whose guild creates ``#orchestrator-<name>``
    (id HOME_CHANNEL) — enough for ``_concierge_create_channel`` and
    ``_concierge_post_to_channel`` to run real code over fake I/O."""
    created = []

    class _FakeGuild:
        async def create_text_channel(self, **kwargs):
            created.append(kwargs)
            return SimpleNamespace(id=HOME_CHANNEL)

    guild = _FakeGuild()
    home = home_channel_stub if home_channel_stub is not None else (
        _RecordingChannel(category_id=MANAGED_CATEGORY,
                           channel_id=int(HOME_CHANNEL)))

    def _get_channel(cid):
        if str(cid) == HOME_CHANNEL:
            return home
        return SimpleNamespace(id=str(cid))  # a category object

    rig.adapter._client = SimpleNamespace(
        get_guild=lambda i: guild, guilds=[guild], get_channel=_get_channel)
    return SimpleNamespace(guild=guild, home=home, created=created)


# ── Admission branch (c): the users section admits the orchestrator stamp ───


def test_users_section_orchestrator_stamp_admits(concierge_rig):
    """THE branch (c) contract: an UNBOUND channel's source, re-homed by the
    wrap to the author's registry orchestrator, resolves — the users section
    is a routing source of truth the admission resolver honors (the registry,
    never config routes)."""
    rig = concierge_rig
    _give_user_orchestrator(rig.root)

    source = _source(rig.adapter)
    assert source.profile is None  # unbound: the wrap does the re-home, not routing
    source.profile = ORCH  # exactly what the wrap's fallback branch does

    assert rig.adapter._concierge_dynamic_runtime_profile(source, {}) == ORCH


def test_users_section_refuses_a_non_matching_stamp(concierge_rig):
    """Anti-spoof parity with the binding/room branches: a stamp the users
    section does not confirm (another bot's profile, a stale name) resolves to
    None — admission keeps its owner fallback and the guard refuses."""
    rig = concierge_rig
    _give_user_orchestrator(rig.root)

    source = _source(rig.adapter)
    source.profile = BOUND_BOT  # a real profile, but not the author's orchestrator

    assert rig.adapter._concierge_dynamic_runtime_profile(source, {}) is None


def test_users_section_branch_needs_a_user_id(concierge_rig):
    """A source with no author id cannot be confirmed against the users
    section — None, fail-closed (no KeyError-shaped crashes on odd sources)."""
    rig = concierge_rig
    _give_user_orchestrator(rig.root)
    source = _source(rig.adapter, user_id=None)
    source.profile = ORCH

    assert rig.adapter._concierge_dynamic_runtime_profile(source, {}) is None


# ── Stamping: the registry-verdict tail ──────────────────────────────────────


def test_unbound_source_gets_the_orchestrator_stamp(concierge_rig):
    """THE stamping contract: an unbound channel's author with a LIVE
    orchestrator gets the process-local stamp — and NO re-home at the verdict
    tail (the category check needs the raw channel, on the event)."""
    rig = concierge_rig
    _set_concierge_config(rig.root, enabled=True)
    _give_user_orchestrator(rig.root)

    source = _source(rig.adapter)

    assert source.profile is None  # no re-home at build_source time
    assert getattr(source, concierge_mixin._CONCIERGE_ORCHESTRATOR_ATTR) == ORCH


def test_dead_orchestrator_never_stamps_and_warns_once(concierge_rig, caplog):
    """The ``_profile_exists`` posture: a registry orchestrator whose profile
    dir is gone (retired under us) never stamps — warn once, hub behavior."""
    from plugins.platforms.discord.concierge_mixin import ConciergeDiscordMixin

    rig = concierge_rig
    _set_concierge_config(rig.root, enabled=True)
    registry.set_user("u1", orchestrator=DEAD_ORCH, guild_id="42")

    ConciergeDiscordMixin._concierge_warned.clear()
    try:
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                source = _source(rig.adapter)
                assert getattr(
                    source, concierge_mixin._CONCIERGE_ORCHESTRATOR_ATTR,
                    None) is None
                assert source.profile is None
        assert caplog.text.count(DEAD_ORCH) == 1
    finally:
        ConciergeDiscordMixin._concierge_warned.clear()


# ── Consumption: the wrap's unbound→orchestrator fallback ───────────────────


def test_unbound_managed_message_rehomes_to_the_orchestrator(
        concierge_rig, monkeypatch):
    """THE fallback contract: a human-authored message in an UNBOUND channel
    inside a managed category, from an author with a live orchestrator, is
    re-homed BEFORE delegation — the delegated turn runs on the orchestrator's
    lane (admission confirms via branch (c))."""
    rig = concierge_rig
    _set_concierge_config(rig.root, enabled=True)
    _give_user_orchestrator(rig.root)
    order = []
    _install_fake_turn_spawner(monkeypatch, rig.adapter, order)
    channel = _RecordingChannel()

    _run_dispatch(rig.adapter, _orch_event(rig.adapter, channel=channel))

    dispatched = [e for e in order if e[0] == "dispatch"]
    assert len(dispatched) == 1
    assert dispatched[0][2] == ORCH            # the delegation saw the mutation
    assert dispatched[0][1].startswith(f"agent:{ORCH}:")  # the orchestrator's lane
    assert channel.sent == []                  # no bootstrap side effects


def test_message_outside_managed_categories_keeps_the_hub(
        concierge_rig, monkeypatch):
    """The category gate: the same message in a category the concierge does
    NOT manage never re-homes (the stamp may land — it is category-blind — but
    the wrap's category check blocks the re-home), and never bootstraps."""
    rig = concierge_rig
    _set_concierge_config(rig.root, enabled=True)
    _give_user_orchestrator(rig.root)
    order = []
    _install_fake_turn_spawner(monkeypatch, rig.adapter, order)
    provision_calls = _recording_provision(monkeypatch, rig)
    channel = _RecordingChannel(category_id=UNMANAGED_CATEGORY)

    _run_dispatch(rig.adapter, _orch_event(rig.adapter, channel=channel))

    dispatched = [e for e in order if e[0] == "dispatch"]
    assert len(dispatched) == 1
    assert dispatched[0][2] is None            # hub behavior, unchanged
    assert provision_calls == []               # no bootstrap either


def test_category_unresolvable_event_is_unchanged(
        concierge_rig, monkeypatch):
    """Fail-closed on an event whose channel carries no resolvable category
    (no raw message at all): unchanged hub behavior, no re-home, no
    bootstrap."""
    rig = concierge_rig
    _set_concierge_config(rig.root, enabled=True)
    _give_user_orchestrator(rig.root)
    order = []
    _install_fake_turn_spawner(monkeypatch, rig.adapter, order)
    provision_calls = _recording_provision(monkeypatch, rig)

    _run_dispatch(rig.adapter, _orch_event(rig.adapter, channel=None))

    dispatched = [e for e in order if e[0] == "dispatch"]
    assert len(dispatched) == 1
    assert dispatched[0][2] is None
    assert provision_calls == []


def test_bound_channel_never_rehomes_via_the_fallback(
        concierge_rig, monkeypatch):
    """By-construction guarantee, asserted anyway (the brief's subtlety): a
    BOUND channel's binding verdict short-circuits the registry-verdict tail,
    so the orchestrator stamp never lands and the fallback can never re-home
    — the bound bot answers, even when the author has an orchestrator."""
    rig = concierge_rig
    _set_concierge_config(rig.root, enabled=True)
    _give_user_orchestrator(rig.root)
    registry.bind_channel("100", profile=BOUND_BOT, blueprint="reminders",
                          guild_id="42")
    order = []
    _install_fake_turn_spawner(monkeypatch, rig.adapter, order)
    provision_calls = _recording_provision(monkeypatch, rig)
    channel = _RecordingChannel()

    event = _orch_event(rig.adapter, channel=channel)
    _run_dispatch(rig.adapter, event)

    dispatched = [e for e in order if e[0] == "dispatch"]
    assert len(dispatched) == 1
    assert dispatched[0][2] == BOUND_BOT        # the binding, not the orchestrator
    assert getattr(event.source,
                   concierge_mixin._CONCIERGE_ORCHESTRATOR_ATTR, None) is None
    assert provision_calls == []                # bound ⇒ no bootstrap either


def test_bot_author_never_rehomes_nor_bootstraps(concierge_rig, monkeypatch):
    """Anti-loop, both halves: a bot-authored message never gets the stamp
    (so never re-homes to the orchestrator) and never triggers the bootstrap
    — a provisioned bot posting in a managed channel must not spawn work."""
    rig = concierge_rig
    _set_concierge_config(rig.root, enabled=True)
    _give_user_orchestrator(rig.root)           # u1 HAS an orchestrator (stamp half)
    order = []
    _install_fake_turn_spawner(monkeypatch, rig.adapter, order)
    provision_calls = _recording_provision(monkeypatch, rig)
    channel = _RecordingChannel()

    event = _orch_event(rig.adapter, channel=channel, is_bot=True)
    _run_dispatch(rig.adapter, event)

    dispatched = [e for e in order if e[0] == "dispatch"]
    assert len(dispatched) == 1
    assert dispatched[0][2] is None             # the hub, never the orchestrator
    assert getattr(event.source,
                   concierge_mixin._CONCIERGE_ORCHESTRATOR_ATTR, None) is None
    assert provision_calls == []                # and never a bootstrap


# ── First-contact bootstrap (design §4) ──────────────────────────────────────


def test_bootstrap_happy_path_provisions_channel_binds_and_notices(
        concierge_rig, monkeypatch):
    """THE bootstrap contract: a human-authored, managed-category, UNBOUND
    first contact from an author with NO orchestrator spawns ONE background
    bootstrap — provision (user-keyed, channel-less), a personal channel in
    the trigger channel's category, home_channel + binding, hub-created
    suppression, a greeting in the new home and a one-line notice in the
    trigger channel — while the TRIGGERING message still answers as the hub,
    and the NEXT message routes to the orchestrator."""
    rig = concierge_rig
    _set_concierge_config(rig.root, enabled=True)
    order = []
    _install_fake_turn_spawner(monkeypatch, rig.adapter, order)
    provision_calls = _recording_provision(monkeypatch, rig)
    fakes = _fake_discord_client(rig)
    channel = _RecordingChannel()

    _run_dispatch(rig.adapter, _orch_event(
        rig.adapter, channel=channel, user_name="Alice"))

    # the triggering message answered as the HUB (fail-closed: bootstrap after)
    dispatched = [e for e in order if e[0] == "dispatch"]
    assert [e[2] for e in dispatched] == [None]
    # provision: user-keyed, channel-less, in the trigger's guild
    assert len(provision_calls) == 1
    assert provision_calls[0] == {"blueprint": "orchestrator",
                                  "channel_id": None,
                                  "user_id": "u1", "guild_id": "42"}
    # the personal channel: orchestrator-slug name, in the managed category
    assert len(fakes.created) == 1
    assert fakes.created[0]["name"] == "orchestrator-alice"
    # the registry: home channel + binding, source of truth for the next message
    assert registry.get_user("u1")["home_channel"] == HOME_CHANNEL
    binding = registry.get_binding(HOME_CHANNEL)
    assert binding["profile"] == ORCH
    assert binding["blueprint"] == "orchestrator"
    assert binding["guild_id"] == "42"
    # the create event's auto-offer picker is suppressed on the new channel
    assert rig.adapter._concierge_channel_is_hub_created(HOME_CHANNEL) is True
    # notices: a greeting in the home channel, a pointer in the trigger channel
    assert len(fakes.home.sent) == 1 and fakes.home.sent[0]
    assert len(channel.sent) == 1
    assert f"<#{HOME_CHANNEL}>" in channel.sent[0]
    # single-flight state was released
    assert not getattr(rig.adapter, "_concierge_bootstrap_inflight", set())

    # the NEXT message routes to the orchestrator (the fallback), no re-provision
    order.clear()
    _run_dispatch(rig.adapter, _orch_event(
        rig.adapter, channel=channel, user_name="Alice"))
    dispatched = [e for e in order if e[0] == "dispatch"]
    assert [e[2] for e in dispatched] == [ORCH]
    assert len(provision_calls) == 1


def test_bootstrap_is_single_flight_per_user(concierge_rig, monkeypatch):
    """Concurrency: a burst of first messages spawns ONE bootstrap — the
    per-user in-flight set gates the second spawn while the first runs (and
    the task re-checks the users section inside, the second backstop)."""
    rig = concierge_rig
    _set_concierge_config(rig.root, enabled=True)
    order = []
    _install_fake_turn_spawner(monkeypatch, rig.adapter, order)
    release = threading.Event()
    provision_calls = _recording_provision(monkeypatch, rig, block_on=release)
    channel_a = _RecordingChannel()
    channel_b = _RecordingChannel(category_id=MANAGED_CATEGORY, channel_id=101)

    async def _scenario():
        # first contact: spawns the bootstrap (blocked inside provision)
        await rig.adapter._concierge_inbound_dispatch(
            _orch_event(rig.adapter, channel=channel_a))
        # second contact from the SAME author (another channel), while the
        # bootstrap is still in flight: no second spawn, hub answers
        await rig.adapter._concierge_inbound_dispatch(
            _orch_event(rig.adapter, chat_id="101", channel=channel_b))
        release.set()
        for task in list(getattr(
                rig.adapter, "_concierge_room_fanout_tasks", {}).values()):
            if not task.done():
                await task

    asyncio.new_event_loop().run_until_complete(_scenario())

    assert len(provision_calls) == 1          # ONE bootstrap for the burst
    dispatched = [e for e in order if e[0] == "dispatch"]
    assert [e[2] for e in dispatched] == [None, None]  # both answered as the hub
    assert not getattr(rig.adapter, "_concierge_bootstrap_inflight", set())


def test_bootstrap_recheck_exits_when_an_orchestrator_exists(
        concierge_rig, monkeypatch):
    """The in-task backstop: a bootstrap that lost a race re-checks the users
    section first and exits without provisioning — the winning contact's
    bootstrap already happened."""
    rig = concierge_rig
    _set_concierge_config(rig.root, enabled=True)
    _give_user_orchestrator(rig.root)
    provision_calls = _recording_provision(monkeypatch, rig)
    fakes = _fake_discord_client(rig)
    channel = _RecordingChannel()

    asyncio.new_event_loop().run_until_complete(
        rig.adapter._concierge_orchestrator_bootstrap(
            _orch_event(rig.adapter, channel=channel)))

    assert provision_calls == []            # re-check exited before provisioning
    assert fakes.created == []              # no channel, no writes
    assert registry.get_binding(HOME_CHANNEL) is None


def test_bootstrap_failure_degrades_to_hub_with_warn_once(
        concierge_rig, monkeypatch, caplog):
    """FAIL-CLOSED: a bootstrap that dies (provision failure) warns ONCE per
    user, aborts cleanly (in-flight released, nothing half-written), and the
    triggering message is UNAFFECTED — the hub answers it, and the next
    message retries the bootstrap."""
    from plugins.platforms.discord.concierge_mixin import ConciergeDiscordMixin

    rig = concierge_rig
    _set_concierge_config(rig.root, enabled=True)
    order = []
    _install_fake_turn_spawner(monkeypatch, rig.adapter, order)
    provision_calls = _recording_provision(monkeypatch, rig, fail=True)
    channel = _RecordingChannel()

    ConciergeDiscordMixin._concierge_warned.clear()
    try:
        with caplog.at_level(logging.WARNING):
            _run_dispatch(rig.adapter, _orch_event(rig.adapter, channel=channel))
            _run_dispatch(rig.adapter, _orch_event(rig.adapter, channel=channel))
        # the message answered as the hub BOTH times
        dispatched = [e for e in order if e[0] == "dispatch"]
        assert [e[2] for e in dispatched] == [None, None]
        # the bootstrap retried (in-flight released) but warned ONCE per user
        # (the warning carries the traceback — count the warning LINE, not
        # the exception text inside it)
        assert len(provision_calls) == 2
        assert caplog.text.count(
            "the orchestrator bootstrap for user u1 failed") == 1
        # nothing half-written: no entry, no binding
        assert registry.get_user("u1") is None
        assert registry.get_binding(HOME_CHANNEL) is None
        assert not getattr(rig.adapter, "_concierge_bootstrap_inflight", set())
    finally:
        ConciergeDiscordMixin._concierge_warned.clear()


def test_bootstrap_needs_an_unbound_channel(concierge_rig, monkeypatch):
    """The decision's unbound gate: a channel with a binding OR a room never
    bootstraps — the registry, not the category, owns routing there. (The
    room row is asserted at the DECISION helper directly: a room event's
    fan-out consumes the message before the fallback block in the wrap.)"""
    rig = concierge_rig
    _set_concierge_config(rig.root, enabled=True)
    provision_calls = _recording_provision(monkeypatch, rig)

    # a ROOM channel (no binding, but room-claimed)
    registry.create_room("100", [BOUND_BOT])
    rig.adapter._concierge_maybe_bootstrap_orchestrator(
        _orch_event(rig.adapter, channel=_RecordingChannel()))
    assert provision_calls == []

    # a BOUND channel (the binding path; the wrap row is covered above).
    # A different channel id: channel 100 is still a room (binding XOR room).
    registry.bind_channel("102", profile=BOUND_BOT, blueprint="reminders",
                         guild_id="42")
    rig.adapter._concierge_maybe_bootstrap_orchestrator(
        _orch_event(rig.adapter, chat_id="102", channel=_RecordingChannel()))
    assert provision_calls == []


# ── T4 quality-review fix: bootstrap postures + the one-read hot-path budget ─


def _counting_registry(monkeypatch):
    """Count ``registry.load_registry`` calls while running the real reader —
    what the hot-path budget assertions read: ONE registry read for the
    whole routing decision; the bootstrap decision must not add one for
    sources the routing verdicts already decided."""
    loads = []
    real_load = registry.load_registry

    def _counting_load():
        loads.append(1)
        return real_load()

    monkeypatch.setattr(registry, "load_registry", _counting_load)
    return loads


def test_user_orchestrator_posture_matrix(concierge_rig, monkeypatch):
    """The posture helper's contract, read straight off ONE already-loaded
    users section (no registry I/O of its own): first contact (no section /
    no entry / no orchestrator field), live, ghost, stat_error — the
    postures the bootstrap must NOT treat as first contact are exactly
    ghost and stat_error (live routes via the stamp lane instead)."""
    rig = concierge_rig
    adapter = rig.adapter

    # first contact: no users section at all, an empty one, a missing entry,
    # and an entry without a usable orchestrator field
    assert adapter._concierge_user_orchestrator(
        None, "u1") == (None, "first_contact")
    assert adapter._concierge_user_orchestrator(
        {}, "u1") == (None, "first_contact")
    assert adapter._concierge_user_orchestrator(
        {"u3": {}}, "u3") == (None, "first_contact")
    assert adapter._concierge_user_orchestrator(
        {"u3": {"orchestrator": "  "}}, "u3") == (None, "first_contact")

    # live: an entry over a real profile dir
    _give_user_orchestrator(rig.root)
    users = registry.load_registry().get("users")
    assert adapter._concierge_user_orchestrator(users, "u1") == (ORCH, "live")

    # ghost: an entry whose profile dir is gone (retired under us)
    registry.set_user("u2", orchestrator=DEAD_ORCH, guild_id="42")
    users = registry.load_registry().get("users")
    assert adapter._concierge_user_orchestrator(
        users, "u2") == (DEAD_ORCH, "ghost")

    # stat_error: the liveness check itself failed
    monkeypatch.setattr(concierge_voice, "_profile_exists",
                        lambda profile: (False, "PermissionError(13)"))
    assert adapter._concierge_user_orchestrator(
        users, "u1") == (ORCH, "stat_error")


def test_bootstrap_skips_a_ghost_orchestrator_entry(
        concierge_rig, monkeypatch, caplog):
    """IMPORTANT #1, posture (b): an author whose registry entry points at a
    GONE profile dir must NOT bootstrap — the idempotent
    ``provision.create_bot`` would return the dead entry without rebuilding
    the dir, and the bootstrap would create + bind a fresh home channel
    onto the dead profile (a permanently wedged channel). Warn once per
    user with the actionable fix; the message itself is unaffected (hub)."""
    rig = concierge_rig
    _set_concierge_config(rig.root, enabled=True)
    registry.set_user("u1", orchestrator=DEAD_ORCH, guild_id="42")
    order = []
    _install_fake_turn_spawner(monkeypatch, rig.adapter, order)
    provision_calls = _recording_provision(monkeypatch, rig)
    channel = _RecordingChannel()

    ConciergeDiscordMixin._concierge_warned.clear()
    try:
        with caplog.at_level(logging.WARNING):
            _run_dispatch(rig.adapter, _orch_event(rig.adapter, channel=channel))
            _run_dispatch(rig.adapter, _orch_event(rig.adapter, channel=channel))
        dispatched = [e for e in order if e[0] == "dispatch"]
        assert [e[2] for e in dispatched] == [None, None]  # message unaffected
        assert provision_calls == []                       # NO bootstrap, ever
        assert caplog.text.count("retire the users entry") == 1  # warn-once
    finally:
        ConciergeDiscordMixin._concierge_warned.clear()


def test_bootstrap_skips_an_uncheckable_orchestrator(
        concierge_rig, monkeypatch, caplog):
    """IMPORTANT #1, posture (c): stat_error — the liveness check itself
    failed (EACCES, EIO, a stalled mount). NEVER provision over the
    environmental fault: no bootstrap, one warning pointing at the
    filesystem (not at "re-provision"), message unaffected."""
    rig = concierge_rig
    _set_concierge_config(rig.root, enabled=True)
    _give_user_orchestrator(rig.root)  # a healthy entry — the patch makes it uncheckable
    monkeypatch.setattr(concierge_voice, "_profile_exists",
                        lambda profile: (False, "PermissionError(13, 'EACCES')"))
    order = []
    _install_fake_turn_spawner(monkeypatch, rig.adapter, order)
    provision_calls = _recording_provision(monkeypatch, rig)
    channel = _RecordingChannel()

    ConciergeDiscordMixin._concierge_warned.clear()
    try:
        with caplog.at_level(logging.WARNING):
            _run_dispatch(rig.adapter, _orch_event(rig.adapter, channel=channel))
        dispatched = [e for e in order if e[0] == "dispatch"]
        assert [e[2] for e in dispatched] == [None]  # message unaffected
        assert provision_calls == []                 # NO bootstrap over a fault
        assert caplog.text.count(
            "skipping the first-contact bootstrap") == 1  # warn-once
    finally:
        ConciergeDiscordMixin._concierge_warned.clear()


def test_bound_channel_message_keeps_one_registry_read(
        concierge_rig, monkeypatch):
    """IMPORTANT #2: a BOUND-channel message in a managed category (the
    normal concierge layout) pays exactly ONE registry read —
    build_source's routing read, the pre-T4 count. The bootstrap decision
    must early-exit on the binding verdict already stamped on the source
    (``source.profile`` = the bound bot), before ANY registry work."""
    rig = concierge_rig
    _set_concierge_config(rig.root, enabled=True)
    _give_user_orchestrator(rig.root)
    registry.bind_channel("100", profile=BOUND_BOT, blueprint="reminders",
                          guild_id="42")
    order = []
    _install_fake_turn_spawner(monkeypatch, rig.adapter, order)
    loads = _counting_registry(monkeypatch)
    channel = _RecordingChannel()

    _run_dispatch(rig.adapter, _orch_event(rig.adapter, channel=channel))

    dispatched = [e for e in order if e[0] == "dispatch"]
    assert [e[2] for e in dispatched] == [BOUND_BOT]  # the binding answers
    assert len(loads) == 1  # the routing read ONLY — no bootstrap-decision read


def test_unbound_first_contact_adds_exactly_one_bootstrap_read(
        concierge_rig, monkeypatch):
    """IMPORTANT #2, the other half: an unbound FIRST-CONTACT message may
    load the registry ONCE for the bootstrap decision (routing read + the
    decision read, nothing more) — the users-section posture reuses THAT
    read, never a third ``load_registry`` (the old separate
    ``orchestrator_for_user`` call)."""
    rig = concierge_rig
    _set_concierge_config(rig.root, enabled=True)
    order = []
    _install_fake_turn_spawner(monkeypatch, rig.adapter, order)
    provision_calls = _recording_provision(monkeypatch, rig)
    _fake_discord_client(rig)
    loads = _counting_registry(monkeypatch)
    channel = _RecordingChannel()

    async def _scenario():
        await rig.adapter._concierge_inbound_dispatch(
            _orch_event(rig.adapter, channel=channel))
        # The MESSAGE path is complete here: the routing read + the ONE
        # bootstrap-decision read. The background task's own re-check and
        # the fake provision's registry writes run only once awaited below.
        assert len(loads) == 2
        for task in list(getattr(
                rig.adapter, "_concierge_room_fanout_tasks", {}).values()):
            if not task.done():
                await task

    asyncio.new_event_loop().run_until_complete(_scenario())
    assert len(provision_calls) == 1  # the bootstrap DID run: first contact


def test_room_stamped_source_skips_the_bootstrap_decision_read(
        concierge_rig, monkeypatch):
    """IMPORTANT #2, the room/member lane: a source carrying the
    room-targets stamp never reaches the decision's registry read — the
    fan-out owns the message, and every member-lane turn (a clone stamped
    ``profile = member``) exits the same way on the profile check."""
    rig = concierge_rig
    _set_concierge_config(rig.root, enabled=True)
    registry.create_room("100", [BOUND_BOT])
    loads = _counting_registry(monkeypatch)
    event = _orch_event(rig.adapter, channel=_RecordingChannel())

    assert getattr(event.source, concierge_mixin._ROOM_TARGETS_ATTR)  # a room source
    rig.adapter._concierge_maybe_bootstrap_orchestrator(event)

    assert len(loads) == 1  # build_source's routing read ONLY — decision added none


def test_uncheckable_orchestrator_never_stamps_and_warns_once(
        concierge_rig, monkeypatch, caplog):
    """The review's missing stamp-tail row: an author's orchestrator that
    CANNOT BE CHECKED never stamps — ONE warning with the
    investigate-the-filesystem diagnosis (never "re-provision", which
    would act on the very environmental fault the check died on)."""
    rig = concierge_rig
    _set_concierge_config(rig.root, enabled=True)
    _give_user_orchestrator(rig.root)
    monkeypatch.setattr(concierge_voice, "_profile_exists",
                        lambda profile: (False, "OSError(5, 'EIO')"))

    ConciergeDiscordMixin._concierge_warned.clear()
    try:
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                source = _source(rig.adapter)
                assert getattr(
                    source, concierge_mixin._CONCIERGE_ORCHESTRATOR_ATTR,
                    None) is None
                assert source.profile is None
        assert caplog.text.count("could not be checked") == 1
    finally:
        ConciergeDiscordMixin._concierge_warned.clear()


def test_room_channel_is_never_admitted_via_the_users_branch(concierge_rig):
    """The review's missing admission row: a ROOM channel's stamp is judged
    by the room block ALONE — a stamp that is not a room member must not
    sneak in through the users branch (c), even when it IS the author's
    registry orchestrator: the room's member list owns the channel."""
    rig = concierge_rig
    _give_user_orchestrator(rig.root)            # u1's orchestrator is ORCH
    registry.create_room("100", [BOUND_BOT])     # channel 100 is a room

    source = _source(rig.adapter)
    source.profile = ORCH  # the author's orchestrator, NOT a room member

    assert rig.adapter._concierge_dynamic_runtime_profile(source, {}) is None
