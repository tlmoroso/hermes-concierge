"""A durable-FIFO claim on a registry-routed satellite session must survive a shared-adapter
reconnect that replaces the adapter OBJECT.

The concierge topology: ONE shared Discord bot registered under the hub profile's
``_profile_adapters`` map, satellite bot profiles routed at runtime by the concierge REGISTRY
(``runner._dynamic_runtime_profile``, the duck-typed hook plugins/platforms/discord/
concierge_mixin.py installs) — never by ``gateway.profile_routes``. A native admission's
LiveSession source is built by ``restore_native_source`` (session_authority._admit_native):
its only transport provenance is the ``_transport_adapter_ref`` weakref frozen at admission
time; no RoutingIdentity is pinned on it (``restore_provenance`` stamps the ref and the
authorization home, never the identity — unlike ``_restored_source``, which re-pins).
A forced adapter reconnect replaces the object (``_handle_profile_adapter_fatal_error`` pops
the failed instance; the reconnect task installs a new one under the same profile). The queued
row's own replay re-binds to the CURRENT adapter through ``restore_provenance``/``_binding``
on every claim, but the live source's frozen ref does not: with the old object unregistered
and no identity to fall back to, ``_delivery_adapter_for(live.source)`` ends at the satellite's
empty ``{}`` adapter map — ``_is_shared_bot_satellite`` answers False for a registry-routed
satellite (no ``profile_routes`` entry) — and ``check_native_route`` refuses the head with
``not_found`` on EVERY attempt. ``_claim_with_retry`` has no recovery branch for that reason,
so the FIFO pauses permanently: messages keep committing queued and never run, until /reset.

Production: two concierge satellite conversations paused 'not_found' on 2026-11-17 (sessions
20261115_105050_9afce736 / 20261115_161824_4a05ca45) after the shared Discord adapter hit
'discord_websocket_health_stale' and reconnected with a new adapter instance; sessions created
after the reconnect kept working, because their live sources' frozen refs name the new object.

Real multiplexed runtime (one authority per served profile, real state.db files, the shared
routing store) on a temp HERMES_HOME; the registry hook, the transport home and the shared
bot's adapter map are installed exactly as the concierge mixin and the secondary-adapter
lifecycle do. No model is needed: the claim under test happens before execution.
"""
import asyncio
import contextlib
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest

from tests.gateway.test_session_authorities_multiplex import _reserve_homes, _runner

HUB = "concierge"          # owns the shared Discord bot (a served secondary profile)
SATELLITE = "bot-orchestrator"  # registry-routed: no adapter of its own, no profile_routes entry
CHAT = "1551304278537150584"
TOKEN = "hub-shared-bot-token"   # same credential before and after the reconnect


def _shared_bot(runner, label):
    """A stub Discord adapter registered the way the hub profile's lifecycle registers it."""
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter

    class _Stub(BasePlatformAdapter):
        pass

    _Stub.__abstractmethods__ = frozenset()
    adapter = _Stub.__new__(_Stub)
    adapter.platform, adapter.gateway_runner, adapter.label = Platform.DISCORD, runner, label
    # The hub bot's own user allowlist (production gates Discord via adapter allow_from):
    # under multiplex the env fallbacks are read through the transport profile's scoped
    # secret store, so this — not a monkeypatched env var — is the authorization seam.
    adapter.config = PlatformConfig(enabled=True, extra={"allow_from": "member-1"})
    adapter.allow_from_covers_groups = True
    adapter.config.token = TOKEN  # the connector fingerprint _binding pins across a restart
    adapter._pending_messages, adapter._active_sessions = {}, {}
    adapter.set_owner_profile(HUB)
    return adapter


@asynccontextmanager
async def _served(tmp_path, monkeypatch):
    """A multiplexed gateway in the concierge shape: the launch profile serves the hub (its
    shared Discord bot) and a registry-routed satellite with no adapter of its own."""
    from gateway import run
    from gateway.config import Platform
    from gateway.run_runtime import initialize_gateway_runtime
    from gateway.runtime_ownership import process_ownership
    from gateway.session_ingress_context import register_transport_home

    monkeypatch.setattr(run, "_load_gateway_config", lambda: {"platform_toolsets": {"cli": []}})
    root, homes = _reserve_homes(tmp_path, monkeypatch, names=(HUB, SATELLITE))
    process_ownership.reserve([home for _, home in homes])
    runner = None
    try:
        runner = _runner(root, homes)
        await initialize_gateway_runtime(runner)
        hub_home, satellite_home = homes[1][1], homes[2][1]
        hub = runner.session_authorities.for_home(hub_home)
        bot = _shared_bot(runner, "SHARED_BOT")
        runner._profile_adapters.setdefault(HUB, {})[Platform.DISCORD] = bot
        runner._profile_adapters.setdefault(SATELLITE, {})  # the startup placeholder map
        register_transport_home(runner, HUB, hub_home)
        # The concierge registry hook: the mixin installs this duck-typed resolver at wire
        # time; it returns the source's stamp only while the registry confirms it.
        runner._dynamic_runtime_profile = lambda source, _homes: getattr(source, "profile", None)
        yield SimpleNamespace(runner=runner, hub=hub, bot=bot, hub_home=hub_home,
                              satellite_home=satellite_home)
    finally:
        for authority in list(getattr(runner, "session_authorities", None) or []):
            authority.db.close()
        for _, home in homes:
            process_ownership.release(home)


async def _admit_queued(served, chat_id, message_id, adapter=None):
    """Admit one native satellite turn through the real ingress codec and leave its row
    queued: the drain the admission schedules is cancelled before it first runs, so the
    claim can be driven deterministically. Returns (sid, live)."""
    from gateway.platforms.event import MessageEvent
    from gateway.session_ingress_context import native_callback

    receiving = adapter if adapter is not None else served.bot
    source = receiving.build_source(chat_id=chat_id, chat_type="group", user_id="member-1")
    source.profile = SATELLITE  # the concierge registry's binding stamp (the wrap re-homes)
    event = MessageEvent(text="hello from the bound channel", source=source,
                         message_id=message_id, timestamp=datetime.now())
    with native_callback(served.runner, event, served.hub_home, profile=HUB):
        await served.hub.admit_native(event)
    route = served.runner.session_store._generate_session_key(source)
    sid = served.runner.session_store.lookup_by_session_key(route).session_id
    live = served.hub.sessions[sid]
    assert live is not None and live.task is not None, "the admission did not publish the lane"
    live.task.cancel()  # created, never started: the row stays queued for the explicit claim
    with contextlib.suppress(asyncio.CancelledError):
        await live.task
    return sid, live


@pytest.mark.asyncio
async def test_a_live_satellite_claim_survives_a_shared_adapter_reconnect(tmp_path, monkeypatch):
    """The row's own replay re-binds to the CURRENT registered adapter on every claim; the
    live session's source must not depend on the adapter OBJECT that happened to be
    registered when the session was last admitted. Same credential, same transport profile,
    same route — the FIFO must move."""
    from gateway.config import Platform
    from gateway.session_contract import SessionRef
    from hermes_state_runtime import get_session_admission, list_session_admissions

    async with _served(tmp_path, monkeypatch) as served:
        sid, live = await _admit_queued(served, CHAT, "m1")
        head = next(row for row in list_session_admissions(served.hub.db, session_id=sid)
                    if row["status"] == "queued")

        # The forced reconnect's registry effect: the failed instance is popped and the
        # reconnect task installs a NEW object under the same profile (run_adapters.
        # _handle_profile_adapter_fatal_error + _run_secondary_profile_reconnect).
        replacement = _shared_bot(served.runner, "SHARED_BOT_RECONNECTED")
        served.runner._profile_adapters[HUB][Platform.DISCORD] = replacement

        # The seam: the live source (restore_native_source-built at admission, identity-less,
        # its transport ref frozen on the replaced object) must still name the shared bot.
        adapter = served.runner._adapter_for_source(live.source)
        assert adapter is replacement, (
            "the pre-reconnect live source no longer resolves to the shared bot "
            f"(got {adapter!r}): check_native_route refuses every claim with not_found and "
            "the FIFO pauses permanently")

        # The drain's claim step (gateway/session_authority.py _claim_with_retry) must claim
        # the queued head, not pause the FIFO behind it.
        claimed = await served.hub._claim_with_retry(SessionRef(served.hub.profile_id, sid), live)
        assert claimed is not None, "the FIFO paused instead of claiming its queued head"
        row, first = claimed
        assert row is not None and row["admission_id"] == head["admission_id"], (row, first)
        assert get_session_admission(served.hub.db, admission_id=head["admission_id"])["status"] \
            == "started", "the queued head was not claimed for execution"


@pytest.mark.asyncio
async def test_a_satellite_session_admitted_after_the_reconnect_still_claims(tmp_path, monkeypatch):
    """The control row of the matrix: a session whose live source was captured on the
    CURRENT adapter object claims fine — production kept serving fresh sessions after the
    reconnect while the two pre-reconnect conversations stranded."""
    from gateway.config import Platform
    from gateway.session_contract import SessionRef

    async with _served(tmp_path, monkeypatch) as served:
        replacement = _shared_bot(served.runner, "SHARED_BOT_RECONNECTED")
        served.runner._profile_adapters[HUB][Platform.DISCORD] = replacement
        sid, live = await _admit_queued(served, CHAT, "fresh-1", adapter=replacement)
        claimed = await served.hub._claim_with_retry(SessionRef(served.hub.profile_id, sid), live)
        assert claimed is not None, "a post-reconnect admission failed to claim its own head"
