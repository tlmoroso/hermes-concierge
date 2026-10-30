"""Concierge-bound channels must ADMIT: the registry-stamped profile resolves.

Live bug (2026-10): a Discord message in a concierge-bound channel died at
admission with ``RuntimeStoreError: profile_mismatch``.
``gateway/session_ingress_context.py::_runtime_profile`` resolved the runtime
profile from ``gateway.profile_routes`` alone; the concierge routes per-channel
via ``concierge/registry.json`` (invisible to that resolver), so a
registry-stamped ``source.profile`` ('bot-<blueprint>-<hex>') never matched the
owner fallback and ``_binding``'s guard raised.

The fix is a fail-closed, duck-typed resolver hook: the runner may carry
``_dynamic_runtime_profile`` (installed by the concierge mixin at wire time —
core never imports the plugin layer); it is the LAST resort before the owner
fallback, and the name it returns is only a CANDIDATE — ``_binding`` still
enforces the stamp equality, served-home membership, and authority checks.

Rig: a minimal fake runner (mirrors the admission machinery's runner contract)
plus a real temp registry, the REAL ``ConciergeDiscordMixin`` resolver, and a
real ``DiscordAdapter`` over the conftest discord mock for the wire test.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from concierge import registry
from gateway.config import Platform
from gateway.profile_routing import ProfileRoute
from gateway.session import SessionSource
from hermes_state_runtime import RuntimeStoreError

from tests.gateway.conftest import _ensure_discord_mock

_ensure_discord_mock()

import plugins.platforms.discord.adapter as discord_platform
from plugins.platforms.discord.adapter import DiscordAdapter
from plugins.platforms.discord.concierge_mixin import ConciergeDiscordMixin

from gateway.config import PlatformConfig

OWNER = "concierge"          # the hub profile owning the shared Discord adapter
BOT = "bot-r-x"              # the concierge-provisioned profile bound to BOUND_CHANNEL
OTHER_BOT = "bot-q-y"        # a room member / a differently-bound profile
BOUND_CHANNEL = "700"
ROOM_CHANNEL = "800"
UNBOUND_CHANNEL = "900"


# ── The rig: multiplex fake runner + temp registry ─────────────────────────


def _make_homes(tmp_path):
    """The launch (owner) home plus a served bot home; the registry lives in
    the owner home exactly as a live install lays it out."""
    primary = Path(tmp_path)
    bot_home = primary / "profiles" / BOT
    bot_home.mkdir(parents=True, exist_ok=True)
    return primary, bot_home


def _fake_runner(tmp_path, *, resolver=None, routes=(), enabled=True):
    """The runner contract ``_binding``/``_runtime_profile`` read, minimally."""
    primary, bot_home = _make_homes(tmp_path)
    adapter = SimpleNamespace(name="discord")
    runner = SimpleNamespace(
        config=SimpleNamespace(
            profile_routes=list(routes),
            multiplex_profiles=True,
            _runtime_profile_homes=[(OWNER, str(primary)), (BOT, str(bot_home))],
            concierge={"enabled": enabled} if enabled is not None else None,
        ),
        adapters={Platform.DISCORD: adapter},
        _profile_adapters={},
        # register_transport_home stores resolved paths; _binding compares
        # resolved homes for equality, so the fake must too.
        _native_transport_homes={None: primary.resolve()},
        _adapter_credential_fingerprint=lambda a: "fp:discord",
        _profile_name_for_source=lambda source, adapter_profile=None: None,
    )
    if resolver is not None:
        runner._dynamic_runtime_profile = resolver
    return runner


def _source(**kwargs):
    kwargs.setdefault("platform", Platform.DISCORD)
    kwargs.setdefault("chat_id", BOUND_CHANNEL)
    kwargs.setdefault("chat_type", "channel")
    return SessionSource(**kwargs)


@pytest.fixture
def bound_registry(tmp_path, monkeypatch):
    """A temp HERMES_HOME whose registry binds BOUND_CHANNEL → BOT and makes
    ROOM_CHANNEL a [BOT, OTHER_BOT] room."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.bind_channel(BOUND_CHANNEL, profile=BOT, blueprint="reminders")
    registry.create_room(ROOM_CHANNEL, [BOT, OTHER_BOT])


def _real_resolver():
    """The REAL mixin resolver, resolved lazily so the no-hook test below can
    document the live bug even before the resolver exists."""
    return ConciergeDiscordMixin()._concierge_dynamic_runtime_profile


# ── The live bug and its fix, at the admission seam ────────────────────────


def test_binding_without_resolver_raises_profile_mismatch(tmp_path, bound_registry):
    """The live bug, documented: no hook installed → a registry-bound channel
    resolves to the owner fallback and admission dies at the guard."""
    from gateway.session_ingress_context import _binding

    runner = _fake_runner(tmp_path)  # no _dynamic_runtime_profile
    with pytest.raises(RuntimeStoreError, match="profile_mismatch"):
        _binding(runner, _source(profile=BOT), None)


def test_binding_admits_registry_bound_channel_with_resolver(tmp_path,
                                                             bound_registry,
                                                             monkeypatch):
    """The fix: with the mixin's resolver installed, the SAME source that died
    above binds to the bot's runtime home."""
    from gateway import session_authorities
    from gateway.session_ingress_context import _binding

    monkeypatch.setattr(session_authorities, "authority_for_home",
                        lambda runner, home: object())
    runner = _fake_runner(tmp_path, resolver=_real_resolver())
    _, _, runtime_home, provenance = _binding(runner, _source(profile=BOT), None)
    _, bot_home = _make_homes(tmp_path)
    assert runtime_home == bot_home.resolve()
    assert provenance["runtime_home"] == str(bot_home.resolve())


def test_runtime_profile_resolves_registry_stamp(tmp_path, bound_registry):
    from gateway.session_ingress_context import _runtime_profile

    runner = _fake_runner(tmp_path, resolver=_real_resolver())
    assert _runtime_profile(runner, _source(profile=BOT), None, OWNER,
                            {OWNER: Path(tmp_path), BOT: Path(tmp_path) / "profiles" / BOT},
                            False) == BOT


def test_runtime_profile_resolver_failure_is_none_safe(tmp_path, monkeypatch):
    """A raising resolver must not crash admission: treated as None, the owner
    fallback (and the fail-closed guard) stays in force."""
    from gateway.session_ingress_context import _runtime_profile

    def boom(source, homes):
        raise RuntimeError("registry exploded")

    runner = _fake_runner(tmp_path, resolver=boom)
    assert _runtime_profile(runner, _source(profile=BOT), None, OWNER, {}, False) == OWNER


def test_binding_refuses_a_stamp_the_registry_does_not_confirm(tmp_path,
                                                                monkeypatch):
    """Anti-spoof: the source claims BOT but the registry binds its channel to
    OTHER_BOT — the resolver must answer None and admission must still refuse."""
    from gateway.session_ingress_context import _binding

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.bind_channel(BOUND_CHANNEL, profile=OTHER_BOT, blueprint="q")
    runner = _fake_runner(tmp_path, resolver=_real_resolver())
    with pytest.raises(RuntimeStoreError, match="profile_mismatch"):
        _binding(runner, _source(profile=BOT), None)


def test_binding_refuses_a_resolver_name_the_source_is_not_stamped_with(
        tmp_path, monkeypatch):
    """Fail-closed hook: a (buggy or hostile) resolver naming a profile the
    source is NOT stamped with cannot bypass the guard — admission refuses."""
    from gateway import session_authorities
    from gateway.session_ingress_context import _binding

    monkeypatch.setattr(session_authorities, "authority_for_home",
                        lambda runner, home: object())
    runner = _fake_runner(tmp_path, resolver=lambda source, homes: BOT)
    with pytest.raises(RuntimeStoreError, match="profile_mismatch"):
        _binding(runner, _source(profile=None), None)


def test_config_route_outranks_the_dynamic_resolver(tmp_path, bound_registry):
    """Priority is unchanged: an explicit ``gateway.profile_routes`` match wins
    over the resolver — the hook is the LAST resort, not a new authority."""
    from gateway.session_ingress_context import _runtime_profile

    route = ProfileRoute(name="route-700", platform="discord", profile=OTHER_BOT,
                         chat_id=BOUND_CHANNEL)
    runner = _fake_runner(tmp_path, resolver=_real_resolver(), routes=[route])
    assert _runtime_profile(runner, _source(profile=BOT), None, OWNER, {}, False) == OTHER_BOT


# ── The mixin resolver: a pure function of registry + source ───────────────


def _mixin():
    return ConciergeDiscordMixin()


def test_resolver_bound_channel_returns_stamp(bound_registry):
    assert _mixin()._concierge_dynamic_runtime_profile(
        _source(profile=BOT), {}) == BOT


def test_resolver_thread_of_bound_channel_returns_stamp(bound_registry):
    source = _source(chat_id="701", parent_chat_id=BOUND_CHANNEL,
                     chat_type="thread", profile=BOT)
    assert _mixin()._concierge_dynamic_runtime_profile(source, {}) == BOT


def test_resolver_room_member_returns_stamp(bound_registry):
    assert _mixin()._concierge_dynamic_runtime_profile(
        _source(chat_id=ROOM_CHANNEL, profile=OTHER_BOT), {}) == OTHER_BOT


def test_resolver_room_thread_returns_stamp(bound_registry):
    source = _source(chat_id="801", parent_chat_id=ROOM_CHANNEL,
                     chat_type="thread", profile=BOT)
    assert _mixin()._concierge_dynamic_runtime_profile(source, {}) == BOT


def test_resolver_unbound_channel_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert _mixin()._concierge_dynamic_runtime_profile(
        _source(chat_id=UNBOUND_CHANNEL, profile=BOT), {}) is None


def test_resolver_stamp_not_matching_registry_returns_none(bound_registry):
    # OTHER_BOT is a ROOM member, not the single binding of BOUND_CHANNEL.
    assert _mixin()._concierge_dynamic_runtime_profile(
        _source(profile=OTHER_BOT), {}) is None


def test_resolver_ignores_non_discord_sources(bound_registry):
    source = _source(platform=Platform.TELEGRAM, profile=BOT)
    assert _mixin()._concierge_dynamic_runtime_profile(source, {}) is None


def test_resolver_unstamped_source_returns_none(bound_registry):
    assert _mixin()._concierge_dynamic_runtime_profile(_source(profile=None), {}) is None


def test_resolver_unreadable_registry_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "concierge").mkdir(parents=True)
    (tmp_path / "concierge" / "registry.json").write_text("{not json")
    assert _mixin()._concierge_dynamic_runtime_profile(
        _source(profile=BOT), {}) is None


# ── The wire: the mixin installs the resolver on the runner ────────────────


@pytest.fixture
def adapter(monkeypatch):
    """A real DiscordAdapter (concierge mixin in MRO) over the discord mock."""
    for _var in ("DISCORD_REQUIRE_MENTION", "DISCORD_FREE_RESPONSE_CHANNELS"):
        monkeypatch.delenv(_var, raising=False)
    a = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    a._client = SimpleNamespace(user=SimpleNamespace(id=999, bot=True))
    return a


def test_build_source_wires_the_resolver_onto_the_runner(adapter, tmp_path,
                                                         monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = _fake_runner(tmp_path)
    adapter.gateway_runner = runner
    adapter.build_source(chat_id=BOUND_CHANNEL, chat_type="channel")
    assert runner._dynamic_runtime_profile == adapter._concierge_dynamic_runtime_profile


def test_wire_is_idempotent_across_messages(adapter, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = _fake_runner(tmp_path)
    adapter.gateway_runner = runner
    adapter.build_source(chat_id=BOUND_CHANNEL, chat_type="channel")
    first = runner._dynamic_runtime_profile
    adapter.build_source(chat_id=UNBOUND_CHANNEL, chat_type="channel")
    assert runner._dynamic_runtime_profile is first


def test_disabled_concierge_installs_nothing(adapter, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = _fake_runner(tmp_path, enabled=False)
    adapter.gateway_runner = runner
    adapter.build_source(chat_id=BOUND_CHANNEL, chat_type="channel")
    assert getattr(runner, "_dynamic_runtime_profile", None) is None


def test_wire_gates_on_multiplex(adapter, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = _fake_runner(tmp_path)
    runner.config.multiplex_profiles = False
    adapter.gateway_runner = runner
    adapter.build_source(chat_id=BOUND_CHANNEL, chat_type="channel")
    assert getattr(runner, "_dynamic_runtime_profile", None) is None
