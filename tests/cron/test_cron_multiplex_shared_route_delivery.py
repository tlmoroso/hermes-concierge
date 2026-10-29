"""Regression tests for #101113 — a credentialless satellite profile under
``gateway.profile_routes`` delivers cron output through the PRIMARY adapter
for exactly the targets the primary routes to it, and fails closed otherwise.

The multiplex ticker hands such a profile a ``SharedRouteAdapters`` view over
the primary adapter map; ``_deliver_result`` resolves a transport from it per
target using the same ``ProfileRoute.matches`` predicate as inbound routing.
"""
import asyncio
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import hermes_yaml as yaml

from agent.secret_scope import is_multiplex_active, set_multiplex_active
from cron.scheduler import _deliver_result
from cron.scheduler_preflight import SharedRouteAdapters, _primary_profile_routes_for_current_home
from cron.scheduler_provider import _tick_adapters_for_profile
from gateway.config import Platform, PlatformConfig
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

PRIMARY_YAML = {
    "gateway": {
        "multiplex_profiles": True,
        "profile_routes": [
            {"name": "fit", "platform": "discord", "chat_id": "1543065293755256852", "profile": "fitness"},
            {"name": "off", "platform": "discord", "chat_id": "999", "profile": "fitness", "enabled": False},
            {"name": "other", "platform": "discord", "chat_id": "777", "profile": "other"},
        ],
    }
}


def _job(chat_id: str) -> dict:
    return {"id": "a7ae1520356c", "name": "brief", "deliver": f"discord:{chat_id}"}


def _run(job, adapters):
    """Drive ``_deliver_result`` with a live loop and a real DeliveryRouter."""
    loop = MagicMock()
    loop.is_running.return_value = True

    def fake_run_coro(coro, _loop):
        future = Future()
        future.set_result(asyncio.run(coro))
        return future

    standalone = []

    async def _fake_send_to_platform(platform, pconfig, chat_id, text, **kwargs):
        standalone.append(chat_id)
        return {"success": False, "error": "DISCORD_BOT_TOKEN is not set"}

    config = MagicMock()
    config.platforms = {Platform.DISCORD: PlatformConfig(enabled=True)}
    config.get_home_channel = lambda p: None
    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch("tools.send_message_tool._send_to_platform", _fake_send_to_platform), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
        error = _deliver_result(job, "hello", adapters=adapters, loop=loop)
    return error, standalone


def _primary_adapter():
    adapter = MagicMock()
    adapter.sent = []

    async def send(chat_id, content, metadata=None):
        adapter.sent.append(chat_id)
        return {"success": True, "message_id": "m1"}

    adapter.send = send
    return adapter


def test_satellite_routes_exact_target_through_primary_adapter(tmp_path, monkeypatch):
    root = tmp_path / "root"
    fitness_home = root / "profiles" / "fitness"
    fitness_home.mkdir(parents=True)
    (root / "config.yaml").write_text(yaml.safe_dump(PRIMARY_YAML), encoding="utf-8")
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: root)
    primary = _primary_adapter()

    token = set_hermes_home_override(str(fitness_home))
    try:
        shared = SharedRouteAdapters(
            {Platform.DISCORD: primary}, _primary_profile_routes_for_current_home()
        )
        # exact enabled route → primary adapter sends, no standalone attempt
        error, standalone = _run(_job("1543065293755256852"), shared)
        assert error is None, error
        assert primary.sent == ["1543065293755256852"]
        assert standalone == []

        # unmatched chat, disabled route, route for another profile → the
        # primary bot is NEVER used; delivery stays on the satellite's own
        # (credentialless) standalone path and reports its failure.
        for chat in ("424242", "999", "777"):
            primary.sent.clear()
            error, standalone = _run(_job(chat), shared)
            assert error is not None and "DISCORD_BOT_TOKEN" in error
            assert primary.sent == []
            assert standalone == [chat]
    finally:
        reset_hermes_home_override(token)


def test_shared_view_is_falsy_without_routes_or_primary_adapters():
    assert not SharedRouteAdapters({}, [])
    assert SharedRouteAdapters({Platform.DISCORD: object()}, []).get(Platform.DISCORD) is None


def test_guild_scoped_route_authorizes_cron_target_even_when_satellite_has_no_platform_block(
    tmp_path, monkeypatch,
):
    """The documented Discord route shape is ``guild_id + chat_id``. A cron target carries no guild
    anchor, so the route must be matched on its target-exact discriminators; and the satellite's
    missing/disabled ``platforms.discord`` block must not veto the PRIMARY's authorized transport
    (#89302 sibling) — before, both fell to standalone "DISCORD_BOT_TOKEN is not set"."""
    root = tmp_path / "root"
    sat_home = root / "profiles" / "fitness"
    sat_home.mkdir(parents=True)
    (root / "config.yaml").write_text(yaml.safe_dump({
        "gateway": {"multiplex_profiles": True, "profile_routes": [
            {"platform": "discord", "guild_id": "G1", "chat_id": "C1", "profile": "fitness"}]},
    }), encoding="utf-8")
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: root)
    primary = _primary_adapter()

    token = set_hermes_home_override(str(sat_home))
    try:
        shared = SharedRouteAdapters({Platform.DISCORD: primary}, _primary_profile_routes_for_current_home())
        for satellite_platforms in ({}, {Platform.DISCORD: PlatformConfig(enabled=False)}):
            primary.sent.clear()
            config = MagicMock()
            config.platforms = satellite_platforms
            config.get_home_channel = lambda p: None
            with patch("gateway.config.load_gateway_config", return_value=config):
                error, standalone = _run(_job("C1"), shared)
            assert error is None, error
            assert primary.sent == ["C1"] and standalone == []
    finally:
        reset_hermes_home_override(token)


def test_live_native_adapter_without_platform_block_is_not_treated_as_disabled():
    """#89302: a live native adapter handed in by the gateway is the authorization; an absent
    ``platforms.<p>`` block in the firing profile means "no config", not "disabled"."""
    from cron.scheduler_delivery import _resolve_target_transport

    config = MagicMock()
    config.platforms = {}
    adapter = object()
    resolved, err = _resolve_target_transport(
        {"id": "j"}, Platform.DISCORD, "discord", {"platform": "discord", "chat_id": "C1"},
        {Platform.DISCORD: adapter}, config)
    assert err is None and resolved[2] is adapter and resolved[1].enabled
    # an explicitly disabled block still vetoes
    config.platforms = {Platform.DISCORD: PlatformConfig(enabled=False)}
    resolved, err = _resolve_target_transport(
        {"id": "j"}, Platform.DISCORD, "discord", {"platform": "discord", "chat_id": "C1"},
        {Platform.DISCORD: adapter}, config)
    assert resolved is None and "not configured/enabled" in err


# ── Cross-profile origin delivery (concierge bots) ──────────────────────────────
# A concierge bot holds its routine cron job in ITS OWN store (deliver=origin,
# origin platform discord) but owns no Discord adapter — the hub profile owns the
# shared connection. Under a multiplex gateway the transport OWNER within the same
# process must deliver the job's recorded origin target; enablement is judged by
# the OWNING profile's config (the live adapter IS its authorization), never by the
# job-holding profile's absent platforms block.

_CONCIERGE_ROOT_YAML = {"gateway": {"multiplex_profiles": True}}  # no profile_routes at all


def _origin_job(chat_id: str = "456") -> dict:
    return {
        "id": "4e5e6614f97c", "name": "routine", "deliver": "origin",
        "origin": {"platform": "discord", "chat_id": chat_id},
    }


def _deliver(job, adapters, *, platforms):
    """``_deliver_result`` with an explicit satellite platforms map (the concierge bot's own
    config has NO discord block — the shape that used to fail with "not configured/enabled")."""
    loop = MagicMock()
    loop.is_running.return_value = True

    def fake_run_coro(coro, _loop):
        future = Future()
        future.set_result(asyncio.run(coro))
        return future

    standalone = []

    async def _fake_send_to_platform(platform, pconfig, chat_id, text, **kwargs):
        standalone.append(chat_id)
        return {"success": False, "error": "DISCORD_BOT_TOKEN is not set"}

    config = MagicMock()
    config.platforms = platforms
    config.get_home_channel = lambda p: None
    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch("tools.send_message_tool._send_to_platform", _fake_send_to_platform), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
        error = _deliver_result(job, "hello", adapters=adapters, loop=loop)
    return error, standalone


def _multiplex_env(tmp_path, monkeypatch, bot_name="bot-reminders-041b"):
    """Primary root config (concierge shape: no profile_routes) + HERMES_HOME scoped to the bot."""
    root = tmp_path / "root"
    bot_home = root / "profiles" / bot_name
    bot_home.mkdir(parents=True)
    (root / "config.yaml").write_text(yaml.safe_dump(_CONCIERGE_ROOT_YAML), encoding="utf-8")
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: root)
    token = set_hermes_home_override(str(bot_home))
    return token


def test_multiplex_origin_job_delivers_through_owner_profiles_adapter(tmp_path, monkeypatch):
    """The bot's store is ticked by the multiplex gateway; the HUB (another served profile in
    the same process) owns the only Discord adapter. The deliver=origin job must ride it."""
    token = _multiplex_env(tmp_path, monkeypatch)
    hub = _primary_adapter()
    previous_multiplex = is_multiplex_active()
    set_multiplex_active(True)
    try:
        view = _tick_adapters_for_profile(
            "bot-reminders-041b",
            adapters={},  # launch profile owns no Discord adapter
            profile_adapters={"concierge-hub": {Platform.DISCORD: hub}, "bot-reminders-041b": {}},
            default_profile="default",
        )
        assert isinstance(view, SharedRouteAdapters)
        error, standalone = _deliver(_origin_job(), view, platforms={})
        assert error is None, error
        assert hub.sent == ["456"]
        assert standalone == []
    finally:
        set_multiplex_active(previous_multiplex)
        reset_hermes_home_override(token)


def test_multiplex_origin_job_delivers_when_hub_is_the_launch_profile(tmp_path, monkeypatch):
    """Same fix when the transport owner is the LAUNCH profile: the origin grant must reach the
    primary adapter map even with zero profile_routes configured."""
    token = _multiplex_env(tmp_path, monkeypatch)
    hub = _primary_adapter()
    previous_multiplex = is_multiplex_active()
    set_multiplex_active(True)
    try:
        view = _tick_adapters_for_profile(
            "bot-reminders-041b",
            adapters={Platform.DISCORD: hub},  # hub == launch profile
            profile_adapters={"bot-reminders-041b": {}},
            default_profile="default",
        )
        error, standalone = _deliver(_origin_job(), view, platforms={})
        assert error is None, error
        assert hub.sent == ["456"]
        assert standalone == []
    finally:
        set_multiplex_active(previous_multiplex)
        reset_hermes_home_override(token)


def test_multiplex_hub_path_unchanged(tmp_path, monkeypatch):
    """The hub/launch profile's own tick keeps the plain adapter map — no view, no grant path."""
    token = _multiplex_env(tmp_path, monkeypatch)
    hub = _primary_adapter()
    try:
        own_map = {Platform.DISCORD: hub}
        for hub_profile in (None, "default"):
            view = _tick_adapters_for_profile(
                hub_profile, adapters=own_map,
                profile_adapters={"bot-reminders-041b": {}}, default_profile="default")
            assert view is own_map
            assert not isinstance(view, SharedRouteAdapters)
        # and the hub's own origin job delivers through its own map exactly as before
        error, standalone = _deliver(_origin_job(), own_map, platforms={})
        assert error is None, error
        assert hub.sent == ["456"]
    finally:
        reset_hermes_home_override(token)


def test_multiplex_origin_grant_fails_closed_without_any_adapter(tmp_path, monkeypatch):
    """No adapter for the platform ANYWHERE in the process: the same WARNING
    ("platform 'discord' not configured/enabled") fires — the grant adds reach, never a
    silent bypass."""
    token = _multiplex_env(tmp_path, monkeypatch)
    previous_multiplex = is_multiplex_active()
    set_multiplex_active(True)
    try:
        view = _tick_adapters_for_profile(
            "bot-reminders-041b",
            adapters={Platform.TELEGRAM: object()},  # some OTHER platform's adapter
            profile_adapters={"bot-reminders-041b": {}, "some-other": {Platform.SLACK: object()}},
            default_profile="default",
        )
        error, standalone = _deliver(_origin_job(), view, platforms={})
        assert error is not None and "platform 'discord' not configured/enabled" in error
        assert standalone == []
    finally:
        set_multiplex_active(previous_multiplex)
        reset_hermes_home_override(token)


def test_multiplex_origin_grant_is_scoped_to_origin_targets_only(tmp_path, monkeypatch):
    """A satellite's EXPLICIT ``discord:<chat>`` target still needs a route — the cross-profile
    grant must never hand a credentialless satellite the hub bot for arbitrary chats."""
    token = _multiplex_env(tmp_path, monkeypatch)
    hub = _primary_adapter()
    previous_multiplex = is_multiplex_active()
    set_multiplex_active(True)
    try:
        view = _tick_adapters_for_profile(
            "bot-reminders-041b",
            adapters={Platform.DISCORD: hub},
            profile_adapters={"bot-reminders-041b": {}},
            default_profile="default",
        )
        error, standalone = _deliver(_job("456"), view, platforms={})
        assert error is not None and "not configured/enabled" in error
        assert hub.sent == [] and standalone == []
    finally:
        set_multiplex_active(previous_multiplex)
        reset_hermes_home_override(token)


def test_multiplex_origin_grant_requires_multiplex_process(tmp_path, monkeypatch):
    """A non-multiplex process (single-profile gateway) never grants cross-profile adapters."""
    token = _multiplex_env(tmp_path, monkeypatch)
    hub = _primary_adapter()
    previous_multiplex = is_multiplex_active()
    set_multiplex_active(False)
    try:
        shared = SharedRouteAdapters({Platform.DISCORD: hub}, [], process_adapters={
            "other-profile": {Platform.DISCORD: object()}})
        assert shared.get(Platform.DISCORD, {
            "platform": "discord", "chat_id": "456", "_resolved_from": "origin"}) is None
    finally:
        set_multiplex_active(previous_multiplex)
        reset_hermes_home_override(token)
