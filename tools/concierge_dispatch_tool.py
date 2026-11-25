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

# (adapter, event, hub_home) for the turn currently running through the
# inbound wrap. ContextVar, not a global: concurrent turns must never
# cross-talk, and tool handlers run in a ThreadPoolExecutor whose context is
# propagated from the submitting task
# (tools/thread_context.py::propagate_context_to_thread).
#
# ``hub_home`` is the gateway's Hermes home, captured by the WRAP before the
# agent worker copies the context. gateway/run.py::_profile_runtime_scope sets
# a HERMES_HOME ContextVar override to the *per-bot* profile home inside the
# worker, and concierge.registry.registry_path() resolves get_hermes_home() —
# that profile has no concierge/registry.json, so an eligibility check run
# there reads an EMPTY registry and refuses EVERY dispatch (the 2026-11-25
# "this is not an eligible orchestrator turn" incident). The handler re-pins
# hub_home around the check. Capturing a string is I/O-free; the registry read
# still happens only when the tool is actually called.
_current: ContextVar[Optional[tuple[Any, Any, Optional[str]]]] = ContextVar(
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
def dispatch_context(adapter, event, *, hub_home: Optional[str] = None):
    """Expose (adapter, event, hub_home) to any ``concierge_dispatch`` call
    made during this inbound turn. Set by the Discord wrap; reset in
    ``finally``.

    ``hub_home`` is the gateway/hub Hermes home, captured in the wrap's
    context BEFORE the agent worker overrides HERMES_HOME to the per-bot
    profile. The handler re-pins it around the eligibility check; see
    ``_current``. ``None`` is the tests/legacy fallback (no re-pinning).
    """
    token = _current.set((adapter, event, hub_home))
    try:
        yield
    finally:
        _current.reset(token)


def take_dispatch_intent(event) -> Optional[tuple[str, str]]:
    """Pop the intent recorded by a tool call for *event*. Called by the wrap
    after the turn finishes. An entry can only exist if a tool call succeeded."""
    with _pending_lock:
        return _pending.pop(id(event), None)


# ── Worker reply capture ──────────────────────────────────────────────────
# The dispatch driver needs the worker's FINAL text (an ARTIFACT:<path> line,
# a research answer, ...). Reading it back from Discord channel history is
# UNRELIABLE: a lazily created worker channel can be empty, the worker event
# can inherit the trigger's thread lane so the reply lands elsewhere, or the
# channel object can be unresolvable. 2026-11-25 incident: the artifacts
# worker wrote the file AND answered ARTIFACT:<path>, but the driver scraped
# an empty window and told the user "Files Bot is unavailable".
# Instead the wrap records the response IN-PROCESS, keyed by (profile, chat_id)
# — both sides can compute it and it survives event clones. The history scrape
# remains as a fallback for any path the wrap does not see.
_worker_reply_expected: set[str] = set()
_worker_replies: dict[str, str] = {}
_worker_reply_lock = threading.Lock()


def worker_reply_key(profile: str, chat_id: str) -> str:
    return f"{profile}:{chat_id}"


def expect_worker_reply(profile: str, chat_id: str) -> None:
    """The driver is about to run a worker turn; clear any stale result."""
    key = worker_reply_key(profile, chat_id)
    with _worker_reply_lock:
        _worker_replies.pop(key, None)
        _worker_reply_expected.add(key)


def record_worker_reply(event, response) -> None:
    """Called by the inbound wrap after runner_handler returns. Only an
    EXPECTed worker lane is recorded; every other turn is a no-op."""
    source = getattr(event, "source", None)
    if source is None or not isinstance(response, str) or not response.strip():
        return
    key = worker_reply_key(str(getattr(source, "profile", "") or ""),
                           str(getattr(source, "chat_id", "") or ""))
    with _worker_reply_lock:
        if key in _worker_reply_expected:
            _worker_reply_expected.discard(key)
            _worker_replies[key] = response


def take_worker_reply(profile: str, chat_id: str) -> Optional[str]:
    key = worker_reply_key(profile, chat_id)
    with _worker_reply_lock:
        _worker_reply_expected.discard(key)
        return _worker_replies.pop(key, None)


def _known_workers() -> list[str]:
    from concierge.blueprints import CATALOG

    return [k for k in CATALOG if k != "orchestrator"]


def _handle_dispatch(args: dict, **kwargs) -> str:
    ctx = _current.get()
    if ctx is None:
        return json.dumps({
            "error": "concierge_dispatch is only available during a live "
                     "orchestrator turn. Do not retry."})
    adapter, event, hub_home = ctx
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
        if hub_home is not None:
            # Re-pin the HUB home for the guard. The agent worker overrides
            # HERMES_HOME to the bot profile, whose registry.json is absent —
            # without this the guard reads an empty registry and refuses every
            # dispatch (2026-11-25). Scope is restored immediately after.
            from hermes_constants import (
                reset_hermes_home_override, set_hermes_home_override)

            token = set_hermes_home_override(hub_home)
            try:
                eligible = adapter._concierge_dispatch_eligible(event)
            finally:
                reset_hermes_home_override(token)
        else:
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
