"""Tests for the webhook adapter's ``concierge-location`` route kind (Task 10.1).

A route configured with ``handler: concierge-location`` turns a phone location
event (an iOS Shortcuts automation, or the Mac findmy fallback) into an agent
run of the reminder bot bound to ``concierge.location_channel_id`` — through
the SAME auth / rate-limit / filter / idempotency pipeline every other webhook
route kind uses, and the same ``_spawn_agent_run`` the default agent-run kind
uses.

Covers:
- A valid POST runs the location prompt AS the bound bot profile
  (``source.profile``) and records delivery to the bound Discord channel
  through the hub's adapters (delivery profile None = primary lane — the same
  lane the bot's cron origin delivery uses).
- No bound bot / unset channel / garbage payload → 200 ignored (the webhook's
  ignored-event convention), never a run, never a 500.
- Auth is the route's own secret: a wrong plain token (X-Gitlab-Token, the
  mode iOS Shortcuts can produce) is refused 401; the right one is accepted.
- Startup validation rejects unknown handler kinds and handler+cron_job /
  handler+deliver_only combinations.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH

from concierge import registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(routes, runner=None, **extra_kw) -> WebhookAdapter:
    extra = {"host": "127.0.0.1", "port": 0, "routes": routes}
    extra.update(extra_kw)
    config = PlatformConfig(enabled=True, extra=extra)
    adapter = WebhookAdapter(config)
    adapter.gateway_runner = runner
    return adapter


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


async def _drain_background_tasks(adapter: WebhookAdapter) -> None:
    tasks = list(adapter._background_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
def location_home(tmp_path, monkeypatch):
    """Temp home with a live bot profile, a channel binding and a runner stub
    carrying the in-hand concierge section (what the config.yaml bridge
    delivers at boot)."""
    root = tmp_path
    (root / "profiles" / "bot-reminders-a1b2").mkdir(parents=True)
    (root / "profiles" / "bot-reminders-a1b2" / ".env").write_text("K=1\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    registry.bind_channel("100", profile="bot-reminders-a1b2",
                          blueprint="reminders", guild_id="42")
    runner = SimpleNamespace(
        config=SimpleNamespace(concierge={"location_channel_id": "100"}))
    return SimpleNamespace(root=root, runner=runner)


def _location_routes(**route_kw) -> dict:
    route = {"secret": _INSECURE_NO_AUTH, "handler": "concierge-location"}
    route.update(route_kw)
    return {"concierge-location": route}


def _capture_handle_message(adapter):
    events = []

    async def _capture(event):
        events.append(event)

    adapter.handle_message = _capture
    return events


_ENTER_PAYLOAD = json.dumps(
    {"event_type": "location", "user": "tim", "region": "work",
     "event": "enter"}).encode()


# ===================================================================
# The run: prompt, profile, delivery
# ===================================================================

class TestConciergeLocationRun:
    @pytest.mark.asyncio
    async def test_post_runs_prompt_as_bound_bot_delivering_to_channel(self, location_home):
        adapter = _make_adapter(_location_routes(), runner=location_home.runner)
        events = _capture_handle_message(adapter)
        app = _create_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/concierge-location", data=_ENTER_PAYLOAD,
                headers={"Content-Type": "application/json",
                         "X-GitHub-Delivery": "loc-1"})
            assert resp.status == 202
            data = await resp.json()
            assert data["status"] == "accepted"
            assert data["profile"] == "bot-reminders-a1b2"
            await _drain_background_tasks(adapter)

        # The run is the bot's: profile stamped on the source (the existing
        # identity machinery scopes the turn to the bot's home from here).
        assert len(events) == 1
        assert events[0].source.profile == "bot-reminders-a1b2"
        assert "arrived at work" in events[0].text
        # The reply leg egresses through the hub's adapters (profile None =
        # primary lane) to the bound channel — the bot's cron origin delivery
        # shape: {'platform': 'discord', 'chat_id': <channel>}.
        delivery = adapter._delivery_info["webhook:concierge-location:loc-1"]
        assert delivery["deliver"] == "discord"
        assert delivery["deliver_extra"]["chat_id"] == "100"
        assert delivery["profile"] is None

    @pytest.mark.asyncio
    async def test_exit_event_prompt_names_leaving(self, location_home):
        adapter = _make_adapter(_location_routes(), runner=location_home.runner)
        events = _capture_handle_message(adapter)
        app = _create_app(adapter)
        body = json.dumps({"user": "tim", "region": "work", "event": "exit"}).encode()

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/concierge-location", data=body,
                headers={"Content-Type": "application/json",
                         "X-GitHub-Delivery": "loc-2"})
            assert resp.status == 202
            await _drain_background_tasks(adapter)

        assert len(events) == 1
        assert "left work" in events[0].text

    @pytest.mark.asyncio
    async def test_run_uses_config_yaml_when_runner_has_no_section(
            self, location_home):
        """No in-hand section (bridge delivered nothing): the handler falls
        back to the config.yaml load — the bare ``handle_location_event``
        path."""
        (location_home.root / "config.yaml").write_text(
            "concierge:\n  location_channel_id: '100'\n", encoding="utf-8")
        adapter = _make_adapter(_location_routes())  # no runner at all
        events = _capture_handle_message(adapter)
        app = _create_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/concierge-location", data=_ENTER_PAYLOAD,
                headers={"Content-Type": "application/json",
                         "X-GitHub-Delivery": "loc-3"})
            assert resp.status == 202
            await _drain_background_tasks(adapter)

        assert len(events) == 1
        assert events[0].source.profile == "bot-reminders-a1b2"


# ===================================================================
# No target: ignored gracefully, never a run, never a 500
# ===================================================================

class TestConciergeLocationNoTarget:
    @pytest.mark.asyncio
    async def test_no_binding_is_ignored(self, location_home):
        registry.unbind_channel("100")
        adapter = _make_adapter(_location_routes(), runner=location_home.runner)
        events = _capture_handle_message(adapter)
        app = _create_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/concierge-location", data=_ENTER_PAYLOAD,
                headers={"Content-Type": "application/json",
                         "X-GitHub-Delivery": "loc-4"})
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ignored"
            await _drain_background_tasks(adapter)

        assert events == []

    @pytest.mark.asyncio
    async def test_unset_channel_is_ignored(self, location_home):
        runner = SimpleNamespace(config=SimpleNamespace(concierge={}))
        adapter = _make_adapter(_location_routes(), runner=runner)
        events = _capture_handle_message(adapter)
        app = _create_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/concierge-location", data=_ENTER_PAYLOAD,
                headers={"Content-Type": "application/json",
                         "X-GitHub-Delivery": "loc-5"})
            assert resp.status == 200
            assert (await resp.json())["status"] == "ignored"
            await _drain_background_tasks(adapter)

        assert events == []

    @pytest.mark.asyncio
    async def test_garbage_payload_is_ignored(self, location_home):
        adapter = _make_adapter(_location_routes(), runner=location_home.runner)
        events = _capture_handle_message(adapter)
        app = _create_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/concierge-location",
                data=json.dumps({"user": "tim", "region": "work",
                                 "event": "teleported"}).encode(),
                headers={"Content-Type": "application/json",
                         "X-GitHub-Delivery": "loc-6"})
            assert resp.status == 200
            assert (await resp.json())["status"] == "ignored"
            await _drain_background_tasks(adapter)

        assert events == []

    @pytest.mark.asyncio
    async def test_ghost_profile_binding_is_ignored(self, location_home):
        registry.bind_channel("100", profile="bot-ghost-zz9zz",
                              blueprint="reminders", guild_id="42")
        adapter = _make_adapter(_location_routes(), runner=location_home.runner)
        events = _capture_handle_message(adapter)
        app = _create_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/concierge-location", data=_ENTER_PAYLOAD,
                headers={"Content-Type": "application/json",
                         "X-GitHub-Delivery": "loc-7"})
            assert resp.status == 200
            assert (await resp.json())["status"] == "ignored"
            await _drain_background_tasks(adapter)

        assert events == []

    @pytest.mark.asyncio
    async def test_unreadable_registry_is_ignored_not_a_500(self, location_home):
        """M10: a registry that exists but cannot be read (staged as a
        directory clobbering the file — OSError on every platform/user) used
        to raise out of resolve_location_run into the aiohttp handler (500).
        The pure API now absorbs it (M5b posture: warn once + ignored
        fallback), so the route answers 200 ignored like every other
        no-target case."""
        reg = registry.registry_path()
        reg.unlink()
        reg.mkdir()
        adapter = _make_adapter(_location_routes(), runner=location_home.runner)
        events = _capture_handle_message(adapter)
        app = _create_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/concierge-location", data=_ENTER_PAYLOAD,
                headers={"Content-Type": "application/json",
                         "X-GitHub-Delivery": "loc-unreadable"})
            assert resp.status == 200
            assert (await resp.json())["status"] == "ignored"
            await _drain_background_tasks(adapter)

        assert events == []

    @pytest.mark.asyncio
    async def test_handler_route_skips_prompt_render_and_skills(
            self, location_home, monkeypatch):
        """M10: handler-kind routes compute their own prompt and run from the
        payload, so the route template render (a json.dumps of up to 1MB of
        payload) and the skills injection are discarded work — skipped. The
        run still gets the HANDLER's prompt, not the template."""
        routes = _location_routes(prompt="TEMPLATE-{user}",
                                  skills=["nonexistent-skill"])
        adapter = _make_adapter(routes, runner=location_home.runner)
        events = _capture_handle_message(adapter)
        render_calls, skill_calls = [], []

        def _record_render(template, payload, event_type, route_name):
            render_calls.append((template, route_name))
            return template  # pass through: deliver_extra renders stay honest

        def _record_skills(prompt, skills):
            skill_calls.append(skills)
            return prompt

        monkeypatch.setattr(adapter, "_render_prompt", _record_render)
        monkeypatch.setattr(adapter, "_apply_skills", _record_skills)
        app = _create_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/concierge-location", data=_ENTER_PAYLOAD,
                headers={"Content-Type": "application/json",
                         "X-GitHub-Delivery": "loc-skip"})
            assert resp.status == 202
            await _drain_background_tasks(adapter)

        # The route's template never rendered (only _render_delivery_extra's
        # chat_id pass-through, which names no route) and no skills injected.
        assert all(template != "TEMPLATE-{user}" for template, _ in render_calls)
        assert all(name != "concierge-location" for _, name in render_calls)
        assert skill_calls == []
        assert len(events) == 1
        assert "arrived at work" in events[0].text  # the handler's own prompt


# ===================================================================
# Auth: the route's own secret, in the mode a phone can produce
# ===================================================================

class TestConciergeLocationAuth:
    @pytest.mark.asyncio
    async def test_wrong_plain_token_refused(self, location_home):
        adapter = _make_adapter(
            _location_routes(secret="route-secret"), runner=location_home.runner)
        events = _capture_handle_message(adapter)
        app = _create_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/concierge-location", data=_ENTER_PAYLOAD,
                headers={"Content-Type": "application/json",
                         "X-Gitlab-Token": "wrong-token",
                         "X-GitHub-Delivery": "loc-8"})
            assert resp.status == 401
            await _drain_background_tasks(adapter)

        assert events == []

    @pytest.mark.asyncio
    async def test_correct_plain_token_accepted(self, location_home):
        """THE iOS Shortcuts auth mode: Shortcuts cannot compute HMACs, but it
        CAN send a static header — the validator's plain-token comparison
        (X-Gitlab-Token == the route secret) is the route-token mechanism the
        plan's Risk section points at."""
        adapter = _make_adapter(
            _location_routes(secret="route-secret"), runner=location_home.runner)
        events = _capture_handle_message(adapter)
        app = _create_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/concierge-location", data=_ENTER_PAYLOAD,
                headers={"Content-Type": "application/json",
                         "X-Gitlab-Token": "route-secret",
                         "X-GitHub-Delivery": "loc-9"})
            assert resp.status == 202
            await _drain_background_tasks(adapter)

        assert len(events) == 1
        assert events[0].source.profile == "bot-reminders-a1b2"


# ===================================================================
# Startup validation
# ===================================================================

class TestConciergeLocationRouteValidation:
    @pytest.mark.asyncio
    async def test_handler_plus_cron_job_rejected_at_connect(self):
        adapter = _make_adapter(
            _location_routes(cron_job="some-job", secret="s3cret"))
        with pytest.raises(ValueError, match="mutually exclusive"):
            await adapter.connect()

    @pytest.mark.asyncio
    async def test_handler_plus_deliver_only_rejected_at_connect(self):
        adapter = _make_adapter(
            _location_routes(deliver_only=True, deliver="discord", secret="s3cret"))
        with pytest.raises(ValueError, match="mutually exclusive"):
            await adapter.connect()

    @pytest.mark.asyncio
    async def test_unknown_handler_rejected_at_connect(self):
        routes = {"weird": {"secret": "s3cret", "handler": "does-not-exist"}}
        adapter = _make_adapter(routes)
        with pytest.raises(ValueError, match="unknown handler"):
            await adapter.connect()
