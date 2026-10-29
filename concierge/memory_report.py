"""Concierge memory report (Task 9.2): what the bots' shared Honcho workspace
knows about the user, rendered as a short, plain-language message for the
``/bot memory`` slash command.

The bots share the user's Honcho workspace — the hub's config chain decides
which one (``plugins/memory/honcho/client.py::resolve_config_path``:
``$HERMES_HOME/honcho.json`` first), and provisioning copies that file
verbatim into every memory-enabled bot (``concierge/provision.py::
_install_honcho``) — so asking ONE client (the hub's) answers for the whole
team. No workspace id is passed or echoed: the workspace is a property of
the bound client, and the concierge never exposes Hermes internals.

The dialectic API this module codes against (verified in
plugins/memory/honcho, not invented):
- ``HonchoSessionManager`` is the client. ``get_or_create(key)`` (session.py)
  binds the session every query needs — a dialectic query against an
  un-bound session returns "" (``_cached_session`` miss).
- ``dialectic_query(session_key, query, reasoning_level=None, peer="user",
  apply_injection_cap=True, raise_errors=False) -> str`` (session_context.py)
  is the question: Honcho's backend LLM answers about a peer and the call
  RETURNS PLAIN-LANGUAGE TEXT (or "" / an exception per the flags).
- ``get_peer_card(session_key, peer="user") -> list[str]`` — curated facts.
- ``list_conclusions(session_key, query=None, peer="user", limit=20)
  -> [{"id", "content"}]`` — durable saved facts.

Client construction mirrors the provider's own chain exactly
(honcho/__init__.py::_do_session_init): ``HonchoClientConfig.
from_global_config()`` (client.py — the hub's honcho.json) -> ``get_honcho_client``
(client.py) -> ``HonchoSessionManager(honcho=..., config=cfg,
context_tokens=cfg.context_tokens, runtime_user_peer_name=<user id>)``.
``memory_report_client`` is the I/O seam — the command offloads the whole
build (construction + queries are network I/O) via ``asyncio.to_thread``;
``to_thread`` copies the hub's context, so the bound config resolves the
HUB's workspace even while bot turns run in other threads (#69123 discipline).

Failure posture: build_memory_report NEVER raises — any client failure (dead
backend, rejected credentials, an unresolved peer) is the failure copy, and
no internal detail leaks into user-facing text.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

__all__ = [
    "EMPTY_COPY",
    "FACT_MAX_CHARS",
    "FAILURE_COPY",
    "MAX_CARD_FACTS",
    "MAX_CONCLUSIONS",
    "MEMORY_QUERY",
    "REPORT_HEADER",
    "REPORT_MAX_CHARS",
    "REPORT_SESSION_KEY",
    "SUMMARY_MAX_CHARS",
    "build_memory_report",
    "memory_report_client",
]

logger = logging.getLogger(__name__)

# One stable, dedicated Honcho session for every report: get_or_create is
# idempotent on this key, so repeated /bot memory calls reuse it instead of
# minting orphan sessions, and it never collides with a real conversation
# session key (gateway keys are chat ids; directory names; repo names).
REPORT_SESSION_KEY = "concierge-memory-report"

# The primary question — the plugin's OWN proven prewarm query, verbatim
# (plugins/memory/honcho/__init__.py::_PREWARM_QUERY): asking Honcho what it
# knows about the user is exactly what that query exists for.
MEMORY_QUERY = (
    "Summarize what you know about this user. Focus on preferences, "
    "current projects, and working style."
)

REPORT_HEADER = "🧠 **What your bots remember about you**"

EMPTY_COPY = ("Your bots haven't picked up anything about you yet — "
              "chat with them a bit and check back later.")

FAILURE_COPY = ("I couldn't reach the memory service just now — "
                "try again in a moment.")

# Discord caps one message at 2000 chars; stay under it with room for the
# followup machinery's own framing. Per-section budgets keep a huge dialectic
# answer from starving the structured sections below it — the whole-report
# cap is the backstop, not the primary bound.
REPORT_MAX_CHARS = 1900
SUMMARY_MAX_CHARS = 1200
FACT_MAX_CHARS = 200
MAX_CARD_FACTS = 8
MAX_CONCLUSIONS = 6


def _clean_lines(values: Iterable[Any]) -> list:
    """Stripped non-blank strings, deduped, order preserved."""
    seen: list = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def _trim(text: str, budget: int) -> str:
    """Clip to ``budget`` on a word boundary with an ellipsis."""
    if len(text) <= budget:
        return text
    return text[: budget - 1].rsplit(" ", 1)[0] + "…"


def build_memory_report(client: Any, *,
                        session_key: str = REPORT_SESSION_KEY) -> str:
    """Render what the shared workspace knows about the user, from a
    dialectic client (``HonchoSessionManager``-shaped; tests stub it).

    One dedicated session, three kinds of knowledge — the dialectic answer
    as the body, curated peer-card facts and saved conclusions as bullet
    sections — bounded short, never a traceback on failure.
    """
    try:
        client.get_or_create(session_key)
        # raise_errors=True: a dead backend must be distinguishable from an
        # honest "nothing learned yet" (#36098). apply_injection_cap=False:
        # the report wants the full answer — the 600-char clip exists for
        # automatic prompt injection, and explicit calls bypass it.
        summary = str(client.dialectic_query(
            session_key, MEMORY_QUERY, peer="user",
            apply_injection_cap=False, raise_errors=True) or "").strip()
        card = _clean_lines(client.get_peer_card(session_key, peer="user"))
        conclusions = _clean_lines(
            (c or {}).get("content")
            for c in (client.list_conclusions(session_key, peer="user") or [])
            if isinstance(c, dict))
    except Exception as exc:
        logger.warning("concierge: memory report query failed: %s", exc)
        return FAILURE_COPY

    if not (summary or card or conclusions):
        return EMPTY_COPY

    sections = [REPORT_HEADER]
    if summary:
        sections.append(_trim(summary, SUMMARY_MAX_CHARS))
    if card:
        sections.append("**Known facts**\n" + "\n".join(
            _trim(f"• {fact}", FACT_MAX_CHARS) for fact in card[:MAX_CARD_FACTS]))
    if conclusions:
        sections.append("**Saved conclusions**\n" + "\n".join(
            _trim(f"• {fact}", FACT_MAX_CHARS)
            for fact in conclusions[:MAX_CONCLUSIONS]))
    return _trim("\n\n".join(sections), REPORT_MAX_CHARS)


def memory_report_client(*, user_id: Optional[str] = None) -> Any:
    """The REAL dialectic client for the report — the HUB's Honcho, built
    exactly the way the provider builds its own (``_do_session_init``):
    config from the hub's honcho.json chain -> cached SDK client ->
    session manager carrying the invoking user's runtime identity (so the
    user peer resolves the same way a gateway turn's does — peerName pin,
    alias map, or the transport id; honcho/session_peers.py).

    NETWORK I/O (client construction may refresh OAuth tokens): call via
    ``asyncio.to_thread``. Raises when Honcho is not configured — the
    command's failure copy answers that.
    """
    from plugins.memory.honcho.client import (
        HonchoClientConfig, get_honcho_client,
    )
    from plugins.memory.honcho.session import HonchoSessionManager

    cfg = HonchoClientConfig.from_global_config()
    # The plugin's own availability gate (honcho/__init__.py::_cfg_usable):
    # enabled with a credential or a self-hosted URL to talk to.
    if not (cfg.enabled and (cfg.api_key or cfg.base_url)):
        raise RuntimeError("Honcho memory is not configured for this install")
    runtime_id = str(user_id or "").strip() or None
    return HonchoSessionManager(
        honcho=get_honcho_client(cfg), config=cfg,
        context_tokens=cfg.context_tokens,
        runtime_user_peer_name=runtime_id,
    )
