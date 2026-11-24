"""Schema-backed orchestrator -> worker dispatch (concierge).

Historically dispatch was a magic plain-text directive (``@dispatch <key>
<task>``) parsed out of the assistant's FINAL reply. Models routinely tried to
CALL it as a tool ("Tool '@dispatch developer ...' doesn't exist"), wrote prose
around it, or produced a 2,000-char directive. This module makes it a REAL
tool with a JSON schema.

The handler runs inside the agent's turn (in a thread pool with
``propagate_context_to_thread``, so a ContextVar set by the Discord wrap is
visible here). It validates the blueprint against the live catalog, checks the
shared dispatch guards, and records the intent in a process-local slot keyed by
the live MessageEvent. The Discord inbound wrap pops that intent AFTER
``runner_handler`` returns, suppresses the model's final text (if any), and
spawns the background driver — the same driver the text directive used.

The plain-text directive remains in the wrap as a LEGACY FALLBACK for profiles
whose SOUL.md has not yet been refreshed; new SOULs instruct the tool only.
"""
from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Optional

from tools.registry import registry

logger = logging.getLogger(__name__)

DISPATCH_TOOL_NAME = "concierge_dispatch"

# (adapter, event) for the turn currently running through the inbound wrap.
# ContextVar, not a global: concurrent turns must never cross-talk, and tool
# handlers run in a ThreadPoolExecutor whose context is propagated from the
# submitting task (tools/thread_context.py::propagate_context_to_thread).
_current: ContextVar[Optional[tuple[Any, Any]]] = ContextVar(
    "concierge_dispatch_context", default=None)

# id(event) -> (blueprint_key, task). Bounded by in-flight turns; every entry
# is popped by the wrap in a finally-style path (see take_dispatch_intent).
_pending: dict[int, tuple[str, str]] = {}
_pending_lock = threading.Lock()

# Static for prompt-cache stability; a test asserts it matches the live
# CATALOG, and the handler re-validates against CATALOG at call time.
_WORKER_KEYS = (
    "reminders", "scheduler", "researcher", "email_triage", "notes",
    "artifacts", "developer",
)

DISPATCH_SCHEMA = {
    "name": DISPATCH_TOOL_NAME,
    "description": (
        "Hand a task to one of your helper bots. Call this INSTEAD of writing a "
        "user-facing reply: your reply is suppressed and the helper's result is "
        "summarized back to the user. Use a SHORT, complete instruction (one or "
        "two sentences) — never the user's whole message and never a drafted "
        "answer. Call it ONCE per turn; the helper's result comes back on its own."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "blueprint": {
                "type": "string",
                "enum": list(_WORKER_KEYS),
                "description": "The helper bot that should do the work.",
            },
            "task": {
                "type": "string",
                "description": "A short, complete statement of the work for that helper.",
            },
        },
        "required": ["blueprint", "task"],
        "additionalProperties": False,
    },
}


@contextmanager
def dispatch_context(adapter, event):
    """Expose (adapter, event) to any ``concierge_dispatch`` call made during
    this inbound turn. Set by the Discord wrap; reset in ``finally``."""
    token = _current.set((adapter, event))
    try:
        yield
    finally:
        _current.reset(token)


def take_dispatch_intent(event) -> Optional[tuple[str, str]]:
    """Pop the intent recorded by a tool call for *event*. Called by the wrap
    after the turn finishes. An entry can only exist if a tool call succeeded."""
    with _pending_lock:
        return _pending.pop(id(event), None)


def _known_workers() -> list[str]:
    from concierge.blueprints import CATALOG

    return [k for k in CATALOG if k != "orchestrator"]


def _handle_dispatch(args: dict, **kwargs) -> str:
    ctx = _current.get()
    if ctx is None:
        return json.dumps({
            "error": "concierge_dispatch is only available during a live "
                     "orchestrator turn. Do not retry."})
    adapter, event = ctx
    payload = args if isinstance(args, dict) else {}
    blueprint = str(payload.get("blueprint") or "").strip()
    task = str(payload.get("task") or "").strip()

    if not blueprint or not task:
        return json.dumps({"error": "both 'blueprint' and 'task' are required."})
    if blueprint == "orchestrator" or blueprint not in _known_workers():
        return json.dumps({
            "error": f"unknown helper '{blueprint}'. Known: "
                     + ", ".join(_known_workers())})
    try:
        eligible = adapter._concierge_dispatch_eligible(event)
    except Exception as e:
        logger.warning("concierge dispatch eligibility check failed: %s", e)
        return json.dumps({"error": "dispatch is unavailable in this session."})
    if not eligible:
        return json.dumps({
            "error": "this is not an eligible orchestrator turn; answer the "
                     "user directly instead."})

    key = id(event)
    with _pending_lock:
        if key in _pending:
            return json.dumps({
                "error": "a helper has already been dispatched this turn."})
        _pending[key] = (blueprint, task)
    logger.info(
        "[concierge] dispatch tool: %s -> %d-char task", blueprint, len(task))
    return json.dumps({
        "dispatched": True,
        "blueprint": blueprint,
        "note": "Stop here. Do NOT write a user-facing answer — the "
                "orchestration layer will deliver the helper's result.",
    })


registry.register(
    name=DISPATCH_TOOL_NAME,
    toolset="concierge_dispatch",
    schema=DISPATCH_SCHEMA,
    handler=_handle_dispatch,
    emoji="↗️",
)
