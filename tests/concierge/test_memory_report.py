"""Concierge memory report contracts (Task 9.2): ``/bot memory``'s payload.

The bots share the user's Honcho workspace (the hub's config chain), so the
concierge can answer "what do my bots know about me?". The report helper is
PURE-ish: it formats a stubbed dialectic client — NO real Honcho network
calls in these tests.

The API shape these tests pin is the REAL HonchoSessionManager surface
(verified in plugins/memory/honcho, never invented):
- ``get_or_create(session_key)`` (session.py) binds the session every
  query needs (a dialectic query against an un-bound session returns "");
- ``dialectic_query(session_key, query, peer="user", ...,
  raise_errors=True) -> str`` (session_context.py) — the LLM-on-the-backend
  question; a query returns plain-language TEXT;
- ``get_peer_card(session_key, peer="user") -> list[str]`` — curated facts;
- ``list_conclusions(session_key, query=None, peer="user", limit=20)
  -> [{"id", "content"}]`` — durable saved facts.

Copy contracts: empty workspace -> an honest empty message; ANY client
failure -> the failure copy, never a traceback; the primary query is the
plugin's own prewarm question (honcho/__init__.py::_PREWARM_QUERY), asked
with ``raise_errors=True`` so a dead backend is distinguishable from an
empty answer, and ``apply_injection_cap=False`` so the report gets the full
answer instead of the 600-char injection clip.
"""

from types import SimpleNamespace

from concierge import memory_report


class StubDialecticClient:
    """The HonchoSessionManager query surface, stubbed — records every call."""

    def __init__(self, *, summary="", card=(), conclusions=(), error=None):
        self.summary = summary
        self.card = list(card)
        self.conclusions = list(conclusions)
        self.error = error
        self.calls = []

    def get_or_create(self, key, **_kw):
        self.calls.append(("get_or_create", key))
        if self.error:
            raise self.error
        return SimpleNamespace(key=key)

    def dialectic_query(self, session_key, query, **kw):
        self.calls.append(("dialectic_query", session_key, query, kw))
        if self.error:
            raise self.error
        return self.summary

    def get_peer_card(self, session_key, peer="user"):
        self.calls.append(("get_peer_card", session_key, peer))
        if self.error:
            raise self.error
        return list(self.card)

    def list_conclusions(self, session_key, query=None, peer="user", limit=20):
        self.calls.append(("list_conclusions", session_key, peer))
        if self.error:
            raise self.error
        return list(self.conclusions)


def _dialectic_calls(client):
    return [c for c in client.calls if c[0] == "dialectic_query"]


# ── the happy path: the workspace's knowledge, rendered ────────────────────


def test_report_renders_summary_facts_and_conclusions():
    """A workspace that knows things renders all three kinds sensibly: the
    dialectic answer as the body, curated card facts and saved conclusions
    as their own bullet sections under the header."""
    client = StubDialecticClient(
        summary="You prefer short answers and work in Pacific time.",
        card=["Prefers concise replies", "Lives in PDT"],
        conclusions=[{"id": "c1", "content": "Wants at most two pings a day"}])
    report = memory_report.build_memory_report(client)
    assert report.startswith(memory_report.REPORT_HEADER)
    assert "You prefer short answers and work in Pacific time." in report
    assert "Prefers concise replies" in report
    assert "Wants at most two pings a day" in report
    # the sections are named and bulleted
    assert "Known facts" in report and "Saved conclusions" in report
    assert "• Prefers concise replies" in report


def test_report_asks_the_prewarm_question_of_the_user_peer_uncapped():
    """The primary query is the plugin's OWN proven prewarm question
    (honcho/__init__.py::_PREWARM_QUERY), asked about the USER peer, with
    ``raise_errors=True`` (a dead backend must be an error, not an empty
    answer) and ``apply_injection_cap=False`` (the report wants the full
    answer, not the 600-char injection clip explicit tool calls bypass)."""
    client = StubDialecticClient(summary="something")
    memory_report.build_memory_report(client)
    (call,) = _dialectic_calls(client)
    _name, session_key, query, kw = call
    assert query == (
        "Summarize what you know about this user. Focus on preferences, "
        "current projects, and working style.")
    assert kw["peer"] == "user"
    assert kw["raise_errors"] is True
    assert kw["apply_injection_cap"] is False


def test_report_binds_one_stable_session_for_every_query():
    """The whole report runs against ONE dedicated session: get_or_create is
    called exactly once, and every query keys off it. A fresh session per
    query would create orphan Honcho sessions and split the report's view."""
    client = StubDialecticClient(summary="s", card=["f1"],
                                 conclusions=[{"id": "1", "content": "c1"}])
    memory_report.build_memory_report(client)
    binds = [c for c in client.calls if c[0] == "get_or_create"]
    assert len(binds) == 1
    key = binds[0][1]
    assert key == memory_report.REPORT_SESSION_KEY
    assert all(c[1] == key for c in client.calls if c[0] != "get_or_create")


# ── the honest edges: empty and failure ─────────────────────────────────────


def test_report_empty_workspace_is_the_honest_empty_copy():
    """Nothing summarized, no facts, no conclusions: the exact empty copy —
    no header, no empty section skeletons pretending at knowledge."""
    client = StubDialecticClient(summary="", card=[], conclusions=[])
    assert memory_report.build_memory_report(client) == memory_report.EMPTY_COPY
    assert "haven" in memory_report.EMPTY_COPY  # honest, not an error
    assert "error" not in memory_report.EMPTY_COPY.lower()


def test_report_client_failure_is_the_failure_copy_never_a_traceback():
    """ANY client error (dead backend, rejected credentials, an unresolved
    peer) answers with the failure copy: no exception escapes, no internal
    detail leaks into the user-facing text."""
    client = StubDialecticClient(error=RuntimeError("connection refused: honcho down"))
    report = memory_report.build_memory_report(client)
    assert report == memory_report.FAILURE_COPY
    assert "honcho down" not in report


def test_report_partial_knowledge_renders_only_present_sections():
    """Sensitivity to partial data: an empty summary with real facts renders
    the facts WITHOUT an empty body block; conclusions-only renders just
    those. No section skeleton with nothing under it."""
    facts_only = StubDialecticClient(summary="", card=["Fact A"],
                                     conclusions=[])
    report = memory_report.build_memory_report(facts_only)
    assert "Fact A" in report
    assert "Saved conclusions" not in report
    conclusions_only = StubDialecticClient(
        summary="", card=[], conclusions=[{"id": "1", "content": "Concl B"}])
    report = memory_report.build_memory_report(conclusions_only)
    assert "Concl B" in report
    assert "Known facts" not in report


def test_report_is_bounded_short_and_clean():
    """Short by construction: fact bullets capped, conclusions capped, the
    whole report under the Discord message budget, blanks and duplicates
    dropped from the fact lists."""
    client = StubDialecticClient(
        summary="word " * 600,  # ~3000 chars — must be trimmed, not fatal
        card=[f"fact {i}" for i in range(30)] + ["fact 1", "  ", ""],
        conclusions=[{"id": str(i), "content": f"concl {i}"} for i in range(30)])
    report = memory_report.build_memory_report(client)
    assert len(report) <= memory_report.REPORT_MAX_CHARS
    assert report.count("• fact 0\n") <= 1      # dedupe: fact 1 twice, one bullet
    assert "•    \n" not in report              # blanks never become bullets
    fact_lines = [ln for ln in report.splitlines() if ln.startswith("• fact")]
    assert len(fact_lines) == memory_report.MAX_CARD_FACTS
    concl_lines = [ln for ln in report.splitlines() if ln.startswith("• concl")]
    assert len(concl_lines) == memory_report.MAX_CONCLUSIONS
