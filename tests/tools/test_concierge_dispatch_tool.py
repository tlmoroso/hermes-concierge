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
    yield
    with t._pending_lock:
        t._pending.clear()


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
