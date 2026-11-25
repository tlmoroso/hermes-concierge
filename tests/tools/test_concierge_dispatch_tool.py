"""The real (schema-backed) orchestrator -> worker dispatch tool.

Replaces the magic ``@dispatch`` text directive models kept trying to CALL as
a tool. Tests here cover the schema/catalog sync and the handler's validation;
tests/gateway/test_discord_concierge_orchestrator_dispatch.py covers the wrap
consuming the recorded intent end-to-end.
"""
import json
from types import SimpleNamespace

import pytest

from concierge.blueprints import CATALOG
from tools import concierge_dispatch_tool as t
from tools.registry import registry


@pytest.fixture(autouse=True)
def _clear_pending():
    with t._pending_lock:
        t._pending.clear()
    with t._worker_reply_lock:
        t._worker_replies.clear()
        t._worker_reply_expected.clear()
    yield
    with t._pending_lock:
        t._pending.clear()
    with t._worker_reply_lock:
        t._worker_replies.clear()
        t._worker_reply_expected.clear()


class _FakeAdapter:
    def __init__(self, eligible=True):
        self.eligible = eligible
        self.checks = 0

    def _concierge_dispatch_eligible(self, event):
        self.checks += 1
        return self.eligible


def _as_dict(result):
    """registry.dispatch may normalize a result to a dict or leave the JSON
    string as-is; both are fine for these assertions."""
    return result if isinstance(result, dict) else json.loads(result)


def _call(args, adapter=None, event=None):
    """Invoke through the real registry dispatch path (what a tool call does)."""
    if adapter is not None:
        with t.dispatch_context(adapter, event or SimpleNamespace()):
            return registry.dispatch(t.DISPATCH_TOOL_NAME, dict(args))
    return registry.dispatch(t.DISPATCH_TOOL_NAME, dict(args))


def test_schema_enum_matches_the_live_catalog():
    """A new worker blueprint must be added in all three places (CATALOG,
    SOUL catalog, tool enum) — the tests enforce it, so drift is impossible."""
    workers = [k for k in CATALOG if k != "orchestrator"]
    schema = t.DISPATCH_SCHEMA
    assert schema["name"] == t.DISPATCH_TOOL_NAME
    assert schema["parameters"]["properties"]["blueprint"]["enum"] == workers
    assert schema["parameters"]["required"] == ["blueprint", "task"]
    assert schema["parameters"]["additionalProperties"] is False


def test_tool_is_registered_in_its_own_toolset():
    entry = registry.get_entry(t.DISPATCH_TOOL_NAME)
    assert entry is not None
    assert entry.toolset == "concierge_dispatch"


def test_valid_call_records_intent_and_returns_an_ack():
    event = SimpleNamespace()
    adapter = _FakeAdapter()
    out = _as_dict(_call({"blueprint": "researcher", "task": "find papers"},
                           adapter, event))
    assert out["dispatched"] is True
    assert out["blueprint"] == "researcher"
    assert "Do NOT write a user-facing answer" in out["note"]
    assert t.take_dispatch_intent(event) == ("researcher", "find papers")
    assert t.take_dispatch_intent(event) is None  # consumed once


def test_unknown_or_self_blueprint_is_rejected():
    for key in ("orchestrator", "nosuchbot"):
        out = _as_dict(_call({"blueprint": key, "task": "x"},
                               _FakeAdapter(), SimpleNamespace()))
        assert "error" in out and "dispatched" not in out


def test_missing_fields_are_rejected():
    for args in ({}, {"blueprint": "researcher"}, {"task": "x"}):
        out = _as_dict(_call(args, _FakeAdapter(), SimpleNamespace()))
        assert "error" in out


def test_call_outside_a_live_turn_is_rejected():
    """No ContextVar -> no (adapter, event): the tool must not silently
    manufacture a dispatch."""
    out = _as_dict(_call({"blueprint": "researcher", "task": "x"}))
    assert "error" in out
    assert "only available during a live orchestrator turn" in out["error"]


def test_ineligible_turn_is_refused():
    adapter = _FakeAdapter(eligible=False)
    out = _as_dict(_call({"blueprint": "researcher", "task": "x"},
                           adapter, SimpleNamespace()))
    assert "error" in out
    assert adapter.checks == 1


def test_only_one_dispatch_per_turn():
    event = SimpleNamespace()
    adapter = _FakeAdapter()
    with t.dispatch_context(adapter, event):
        first = _as_dict(registry.dispatch(
            t.DISPATCH_TOOL_NAME, {"blueprint": "researcher", "task": "a"}))
        second = _as_dict(registry.dispatch(
            t.DISPATCH_TOOL_NAME, {"blueprint": "scheduler", "task": "b"}))
    assert first["dispatched"] is True
    assert "already been dispatched" in second["error"]
    assert t.take_dispatch_intent(event) == ("researcher", "a")


def test_dispatch_tool_is_registered_and_stays_eager():
    """On the VM, tool_search is ACTIVE (snowballstemmer installed). A normal
    non-core toolset would be deferred behind tool_search/tool_call, so the
    model never sees dispatch and the SOUL's 'exactly three tools' is a lie.
    Session-gating the toolset keeps it eager AND out of default composites.
    """
    from model_tools import get_all_tool_names, get_tool_definitions
    from toolsets import TOOLSET_SESSION_PLATFORMS, resolve_toolset
    from tools.tool_search import is_deferrable_tool_name

    assert "concierge_dispatch" in get_all_tool_names()
    assert TOOLSET_SESSION_PLATFORMS.get("concierge_dispatch") == frozenset(
        {"discord"})
    assert "concierge_dispatch" not in resolve_toolset("all")  # no leak
    assert is_deferrable_tool_name("concierge_dispatch") is False
    defs = get_tool_definitions(
        enabled_toolsets=["web", "concierge_dispatch"])
    assert sorted(d["function"]["name"] for d in defs) == [
        "concierge_dispatch", "web_extract", "web_search"]


class _RegistryAwareAdapter(_FakeAdapter):
    """Mimic the real guard: it resolves the registry path from the ambient
    HERMES_HOME ContextVar, so it only passes when the HUB home is pinned."""

    def __init__(self, hub):
        super().__init__()
        self.hub = str(hub)

    def _concierge_dispatch_eligible(self, event):
        from concierge import registry

        self.checks += 1
        return str(registry.registry_path()).startswith(self.hub)


def test_handler_repins_hub_home_around_eligibility(tmp_path):
    """REGRESSION 2026-11-25. gateway/run.py sets a HERMES_HOME ContextVar
    override to the per-bot profile inside the agent worker; that profile has
    no concierge/registry.json, so an eligibility check run there reads an
    EMPTY registry and refuses every dispatch. The wrap captures the hub home
    and the handler re-pins it around the guard.
    """
    from hermes_constants import (
        reset_hermes_home_override, set_hermes_home_override)

    hub = tmp_path / "hub"
    event = SimpleNamespace()
    adapter = _RegistryAwareAdapter(hub)
    token = set_hermes_home_override(str(tmp_path / "bot-orchestrator-xxxx"))
    try:
        with t.dispatch_context(adapter, event, hub_home=str(hub)):
            out = _as_dict(registry.dispatch(
                t.DISPATCH_TOOL_NAME,
                {"blueprint": "researcher", "task": "find papers"}))
    finally:
        reset_hermes_home_override(token)
    assert out["dispatched"] is True
    assert adapter.checks == 1
    assert t.take_dispatch_intent(event) == ("researcher", "find papers")


def test_without_the_hub_pin_the_worker_home_breaks_dispatch(tmp_path):
    """The bug, made explicit: no hub_home => the worker's HERMES_HOME wins
    and the guard refuses the dispatch. Documents why the pin is mandatory."""
    from hermes_constants import (
        reset_hermes_home_override, set_hermes_home_override)

    hub = tmp_path / "hub"
    event = SimpleNamespace()
    adapter = _RegistryAwareAdapter(hub)
    token = set_hermes_home_override(str(tmp_path / "bot-orchestrator-xxxx"))
    try:
        with t.dispatch_context(adapter, event):  # legacy/tests: no pin
            out = _as_dict(registry.dispatch(
                t.DISPATCH_TOOL_NAME,
                {"blueprint": "researcher", "task": "find papers"}))
    finally:
        reset_hermes_home_override(token)
    assert "not an eligible orchestrator turn" in out["error"]
    assert t.take_dispatch_intent(event) is None


def test_worker_reply_capture_roundtrip():
    """The driver's reliable replacement for scraping Discord history."""
    t.expect_worker_reply("bot-artifacts-x", "700")
    event = SimpleNamespace(source=SimpleNamespace(
        profile="bot-artifacts-x", chat_id="700"))
    t.record_worker_reply(event, "ARTIFACT:/root/x/itinerary.md")
    assert t.take_worker_reply("bot-artifacts-x", "700") == (
        "ARTIFACT:/root/x/itinerary.md")
    assert t.take_worker_reply("bot-artifacts-x", "700") is None


def test_worker_reply_capture_ignores_unexpected_turns():
    """A normal orchestrator/user turn must never be mistaken for a worker."""
    event = SimpleNamespace(source=SimpleNamespace(
        profile="bot-orchestrator-x", chat_id="555"))
    t.record_worker_reply(event, "a normal answer")
    assert t.take_worker_reply("bot-orchestrator-x", "555") is None


def test_worker_reply_capture_ignores_empty_responses():
    t.expect_worker_reply("bot-artifacts-x", "700")
    event = SimpleNamespace(source=SimpleNamespace(
        profile="bot-artifacts-x", chat_id="700"))
    t.record_worker_reply(event, "   ")
    assert t.take_worker_reply("bot-artifacts-x", "700") is None
