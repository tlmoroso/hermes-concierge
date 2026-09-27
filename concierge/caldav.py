"""Minimal iCloud CalDAV reader/writer (Task 10.3 — APPLE_BRIDGE.md option (a)).

Apple-only skills (apple-reminders, apple/findmy) cannot run on the Linux VM, but
the DATA is reachable over CalDAV: iCloud Calendar (VEVENT) and Apple Reminders
(VTODO) both sync through Apple's CalDAV server. This module is the minimal
client the cloud bots use: list today's calendar events, list incomplete
reminders, create a reminder.

**iCloud endpoints** (documented, stable):
- Calendar: ``https://caldav.icloud.com/`` — calendar collections under the
  user's principal (discover via ``PROPFIND current-user-principal`` or the
  OS's account settings).
- Reminders: the SAME server and principal — Apple serves VTODO collections
  (the Reminders lists) alongside VEVENT calendars there.
- Auth: the full Apple ID (e.g. ``user@example.com``) plus an **app-specific
  password** — appleid.apple.com → Sign-In and Security → App-Specific
  Passwords (requires 2FA). That is a scoped credential, NOT the iCloud
  login password. It lives in the VM profile's ``.env``:
  ``ICLOUD_USERNAME`` + ``ICLOUD_APP_PASSWORD`` (secrets: .env only, never
  config.yaml, never the repo).

**Dependency decision (TODO):** no CalDAV library is in this repo's
dependencies (pyproject.toml / uv.lock carry neither ``caldav`` nor
``icalendar``; checked 2026-09-21) and we do not vendor an implementation.
This module is therefore a minimal RFC 5545 (iCalendar) + RFC 4791 (CalDAV)
client behind a pluggable transport (``CalDAVTransport``); the default
transport uses ``requests`` (already a core dependency). If discovery or
server-quirk robustness ever outgrows it, adopt the ``caldav`` library behind
the same transport seam (an extra group, lazily installed) — the
parsing/formatting contracts these tests pin must keep passing either way.

Everything here is fixture-testable: parsing and formatting are pure string
functions, and the client issues every request through the injected transport
(``RequestsTransport`` in production, a fake in tests — no network in tests).
"""

from __future__ import annotations

import base64
import logging
import uuid
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "CalendarEvent", "Reminder", "TransportResponse", "CalDAVTransport",
    "RequestsTransport", "CalDAVClient", "MissingCredentials",
    "parse_events", "parse_todos", "escape_text", "format_ical_utc",
    "build_reminder_vtodo", "build_events_time_range_query",
    "build_incomplete_todos_query", "icloud_credentials_from_env",
    "client_from_env",
]

ICLOUD_SERVER_URL = "https://caldav.icloud.com"
#: Secrets live in .env only (repo rule); these are the two keys the bridge reads.
ICLOUD_USERNAME_ENV = "ICLOUD_USERNAME"
ICLOUD_APP_PASSWORD_ENV = "ICLOUD_APP_PASSWORD"

_PRODID = "-//Hermes Concierge//caldav bridge//EN"
_LINE_OCTET_LIMIT = 75  # RFC 5545 §3.1: fold longer lines with CRLF + single space
_ICAL_DT = "%Y%m%dT%H%M%S"
_ICAL_DT_Z = "%Y%m%dT%H%M%SZ"
_ICAL_DATE = "%Y%m%d"


class MissingCredentials(Exception):
    """ICLOUD_USERNAME / ICLOUD_APP_PASSWORD not both present in the environment."""


# ---------------------------------------------------------------------------
# Parsed shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CalendarEvent:
    """One VEVENT. ``start``/``end`` are datetime (naive when the server sent a
    floating/TZID time, UTC-aware when it sent ...Z) or date (VALUE=DATE)."""
    uid: Optional[str]
    summary: str
    start: Any = None
    end: Any = None
    location: Optional[str] = None


@dataclass(frozen=True)
class Reminder:
    """One VTODO. ``completed`` (a datetime) or ``status`` == "COMPLETED" marks
    the reminder done; incomplete is the common case Apple syncs."""
    uid: Optional[str]
    summary: str
    due: Any = None
    completed: Any = None
    status: Optional[str] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# RFC 5545 parsing (pure — fixtures in, dataclasses out, never raises)
# ---------------------------------------------------------------------------

def _unfold(text: str) -> List[str]:
    """Logical lines: RFC 5545 folds long lines with CRLF + space/tab."""
    lines: List[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _split_property(line: str) -> Optional[tuple]:
    """('SUMMARY', {'TZID': ...}, value) — name/params split at the first ':'."""
    head, sep, value = line.partition(":")
    if not sep:
        return None
    parts = head.split(";")
    name, params = parts[0].upper(), {}
    for part in parts[1:]:
        key, _, val = part.partition("=")
        if key:
            params[key.upper()] = val
    return name, params, value


def _unescape_text(value: str) -> str:
    """RFC 5545 §3.3.11 text unescaping (\\, \\; \\n \\\\)."""
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append({"n": "\n", "N": "\n"}.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _parse_ical_dt(value: str) -> Any:
    """UTC datetime, naive datetime (floating/TZID), or date (all-day); None
    when unparseable — a bad stamp never drops the whole component.

    Date-only (``VALUE=DATE``) stamps are discriminated by the value itself —
    eight digits, no ``T`` — because ``_components`` discards property
    params, so they never reach here (verified by execution: the no-``T``
    heuristic parses ``DTSTART;VALUE=DATE:20260921`` to date(2026, 9, 21)).
    """
    value = value.strip()
    try:
        if "T" not in value:  # date-only (VALUE=DATE): no time part
            return datetime.strptime(value, _ICAL_DATE).date()
        if value.endswith("Z"):
            return datetime.strptime(value, _ICAL_DT_Z).replace(tzinfo=timezone.utc)
        return datetime.strptime(value, _ICAL_DT)  # floating / TZID-qualified
    except ValueError:
        return None


def _components(lines: List[str], kind: str) -> List[Dict[str, str]]:
    """Property lists of every BEGIN:<kind>...END:<kind> block, unfolded.

    Lines inside NESTED components (a VTODO's or VEVENT's VALARM) belong to
    the child, not the parent: a depth counter skips them, so a VALARM's
    DESCRIPTION can never clobber the parent's real one (last-write-wins
    would otherwise leak it in — M10).
    """
    blocks, current, depth = [], None, 0
    for line in lines:
        prop = _split_property(line)
        if prop is None:
            continue
        name, params, value = prop
        if current is not None and depth:  # inside a nested sub-component
            if name == "BEGIN":
                depth += 1
            elif name == "END":
                depth -= 1
            continue
        if name == "BEGIN" and value.strip().upper() == kind:
            current = {}
        elif name == "BEGIN" and current is not None:
            depth = 1  # a sub-component just opened inside our block
        elif name == "END" and value.strip().upper() == kind:
            if current is not None:
                blocks.append(current)
            current = None
        elif current is not None:
            current[name] = value  # last-write-wins: overrides win in iCalendar
    return blocks


def parse_events(text: str) -> List[CalendarEvent]:
    """Every VEVENT in an iCalendar payload (garbage/empty → [])."""
    events = []
    for block in _components(_unfold(text), "VEVENT"):
        events.append(CalendarEvent(
            uid=_unescape_text(block.get("UID", "")) or None,
            summary=_unescape_text(block.get("SUMMARY", "")),
            start=_parse_ical_dt(block.get("DTSTART", "")) if "DTSTART" in block else None,
            end=_parse_ical_dt(block.get("DTEND", "")) if "DTEND" in block else None,
            location=_unescape_text(block["LOCATION"]) if "LOCATION" in block else None,
        ))
    return events


def parse_todos(text: str) -> List[Reminder]:
    """Every VTODO in an iCalendar payload (garbage/empty → [])."""
    todos = []
    for block in _components(_unfold(text), "VTODO"):
        todos.append(Reminder(
            uid=_unescape_text(block.get("UID", "")) or None,
            summary=_unescape_text(block.get("SUMMARY", "")),
            due=_parse_ical_dt(block["DUE"]) if "DUE" in block else None,
            completed=(_parse_ical_dt(block["COMPLETED"])
                       if "COMPLETED" in block else None),
            status=block.get("STATUS"),
            notes=_unescape_text(block["DESCRIPTION"])
            if "DESCRIPTION" in block else None,
        ))
    return todos


# ---------------------------------------------------------------------------
# RFC 5545 formatting (pure)
# ---------------------------------------------------------------------------

def escape_text(value: str) -> str:
    """RFC 5545 §3.3.11: escape backslash, semicolon, comma, newline."""
    return (value.replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def format_ical_utc(moment: datetime) -> str:
    """...Z form; a naive datetime is treated as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(_ICAL_DT_Z)


def _fold(text: str) -> str:
    """Fold every logical line to <=75 octets (UTF-8) with CRLF + space. The
    continuation line's leading space COUNTS toward its 75, and folding happens
    on character boundaries so multibyte text is never split mid-codepoint."""
    out_lines: List[str] = []
    for logical in text.replace("\r\n", "\n").split("\n"):
        if len(logical.encode("utf-8")) <= _LINE_OCTET_LIMIT:
            out_lines.append(logical)
            continue
        current, budget = "", _LINE_OCTET_LIMIT
        for ch in logical:
            width = len(ch.encode("utf-8"))
            if budget < width:
                out_lines.append(current)
                current, budget = " ", _LINE_OCTET_LIMIT - 1
            current += ch
            budget -= width
        out_lines.append(current)
    return "\r\n".join(out_lines)


def build_reminder_vtodo(summary: str, *, due: Optional[datetime] = None,
                         notes: Optional[str] = None, uid: Optional[str] = None,
                         dtstamp: Optional[datetime] = None) -> str:
    """The iCalendar body for creating a reminder (POST to a VTODO collection)."""
    stamp = format_ical_utc(dtstamp or datetime.now(timezone.utc))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "BEGIN:VTODO",
        f"UID:{uid or f'{uuid.uuid4()}@hermes-concierge'}",
        f"DTSTAMP:{stamp}",
        f"SUMMARY:{escape_text(summary)}",
    ]
    if due is not None:
        lines.append(f"DUE:{format_ical_utc(due)}")
    if notes:
        lines.append(f"DESCRIPTION:{escape_text(notes)}")
    lines += ["END:VTODO", "END:VCALENDAR"]
    return _fold("\r\n".join(lines))


# ---------------------------------------------------------------------------
# CalDAV REPORT query bodies (pure; RFC 4791 §7.9 / RFC 4791 filter syntax)
# ---------------------------------------------------------------------------

_QUERY_PREAMBLE = ('<?xml version="1.0" encoding="utf-8" ?>\n'
                   '<C:calendar-query xmlns:D="DAV:" '
                   'xmlns:C="urn:ietf:params:xml:ns:caldav">\n'
                   " <D:prop>\n  <D:getetag />\n  <C:calendar-data />\n </D:prop>\n")


def build_events_time_range_query(start: datetime, end: datetime) -> str:
    """calendar-query for VEVENTs overlapping [start, end) — the server filters."""
    return (_QUERY_PREAMBLE +
            " <C:filter>\n  <C:comp-filter name=\"VCALENDAR\">\n"
            "   <C:comp-filter name=\"VEVENT\">\n"
            f'    <C:time-range start="{format_ical_utc(start)}" '
            f'end="{format_ical_utc(end)}" />\n'
            "   </C:comp-filter>\n  </C:comp-filter>\n </C:filter>\n"
            "</C:calendar-query>")


def build_incomplete_todos_query() -> str:
    """calendar-query for VTODOs with no COMPLETED stamp — open reminders."""
    return (_QUERY_PREAMBLE +
            " <C:filter>\n  <C:comp-filter name=\"VCALENDAR\">\n"
            "   <C:comp-filter name=\"VTODO\">\n"
            '    <C:prop-filter name="COMPLETED">\n'
            "     <C:is-not-defined />\n"
            "    </C:prop-filter>\n   </C:comp-filter>\n"
            "  </C:comp-filter>\n </C:filter>\n</C:calendar-query>")


# ---------------------------------------------------------------------------
# Transport seam (production: requests; tests: a fake — no network in tests)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TransportResponse:
    status: int
    body: str
    headers: Dict[str, str] = field(default_factory=dict)


class CalDAVTransport(Protocol):
    """One HTTP round-trip. The ONLY place network lives; everything else is pure."""

    def request(self, method: str, url: str, *, body: Optional[str] = None,
                headers: Optional[Dict[str, str]] = None) -> TransportResponse: ...


class RequestsTransport:
    """Default transport over ``requests`` (a core dependency, exact-pinned).
    Credentials ride as a Basic auth header — the app-specific password never
    appears in a URL or a log line."""

    def __init__(self, username: str, password: str, *, timeout: float = 30.0):
        self._username = username
        self._password = password
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        token = base64.b64encode(
            f"{self._username}:{self._password}".encode()).decode()
        return {"Authorization": f"Basic {token}",
                "Content-Type": "text/calendar; charset=utf-8"}

    def request(self, method: str, url: str, *, body: Optional[str] = None,
                headers: Optional[Dict[str, str]] = None) -> TransportResponse:
        import requests  # core dependency; lazy so pure parsing needs no HTTP stack

        merged = {**self._headers(), **(headers or {})}
        response = requests.request(method, url, data=body, headers=merged,
                                    timeout=self._timeout)
        return TransportResponse(status=response.status_code, body=response.text,
                                 headers=dict(response.headers))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def _calendar_data_bodies(xml_text: str) -> List[str]:
    """calendar-data payloads from a 207 multistatus REPORT response (by local
    name — DAV prefixes vary across servers)."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        logger.debug("caldav: unparseable multistatus body (%d chars)", len(xml_text))
        return []
    bodies = []
    for element in root.iter():
        if isinstance(tag := getattr(element, "tag", ""), str) and \
                tag.rsplit("}", 1)[-1] == "calendar-data" and element.text:
            bodies.append(element.text)
    return bodies


class CalDAVClient:
    """Minimal CalDAV reader/writer: today's events, open reminders, new reminders.

    ``server_url`` is the CalDAV root (e.g. https://caldav.icloud.com); the
    collection URLs (the calendar / reminders list paths under the user's
    principal) are passed per call — the VM profile documents them once.
    """

    def __init__(self, server_url: str, username: str, password: str,
                 transport: Optional[CalDAVTransport] = None):
        self.server_url = server_url.rstrip("/")
        self.username = username
        self.password = password
        self._transport: CalDAVTransport = (
            transport or RequestsTransport(username, password))

    def _url(self, path: str) -> str:
        return path if path.startswith(("http://", "https://")) \
            else f"{self.server_url}/{path.lstrip('/')}"

    def _report(self, url: str, query_body: str) -> List[str]:
        response = self._transport.request(
            "REPORT", self._url(url), body=query_body, headers={"Depth": "1"})
        if response.status not in (200, 207):
            logger.warning("caldav: REPORT %s -> %d", url, response.status)
            return []
        return _calendar_data_bodies(response.body)

    def list_today_events(self, calendar_url: str,
                          today: Optional[date] = None) -> List[CalendarEvent]:
        """Events overlapping today (the server's time-range filter)."""
        day = today or datetime.now(timezone.utc).date()
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        events: List[CalendarEvent] = []
        for body in self._report(calendar_url,
                                 build_events_time_range_query(start, end)):
            events.extend(parse_events(body))
        return events

    def list_incomplete_reminders(self, reminders_url: str) -> List[Reminder]:
        """Open reminders (no COMPLETED stamp). The server filter is primary;
        the client re-filters because Apple occasionally returns completed
        VTODOs alongside."""
        reminders: List[Reminder] = []
        for body in self._report(reminders_url, build_incomplete_todos_query()):
            reminders.extend(parse_todos(body))
        return [r for r in reminders
                if r.completed is None and (r.status or "").upper() != "COMPLETED"]

    def create_reminder(self, reminders_url: str, summary: str, *,
                        due: Optional[datetime] = None,
                        notes: Optional[str] = None) -> bool:
        """POST a new VTODO to the reminders collection; True on any 2xx."""
        body = build_reminder_vtodo(summary, due=due, notes=notes)
        response = self._transport.request(
            "POST", self._url(reminders_url), body=body,
            headers={"Content-Type": "text/calendar; charset=utf-8"})
        if not 200 <= response.status < 300:
            logger.warning("caldav: POST reminder to %s -> %d",
                           reminders_url, response.status)
            return False
        return True


# ---------------------------------------------------------------------------
# Credentials — env only, never hardcoded
# ---------------------------------------------------------------------------

def icloud_credentials_from_env(env: Optional[Mapping[str, str]] = None) -> Optional[Dict[str, str]]:
    """iCloud CalDAV credentials from the environment (``.env``-sourced at boot):
    ``ICLOUD_USERNAME`` + ``ICLOUD_APP_PASSWORD``. None when either is missing —
    the caller decides between "no bridge" and a loud error."""
    import os

    env = os.environ if env is None else env
    username = (env.get(ICLOUD_USERNAME_ENV) or "").strip()
    password = (env.get(ICLOUD_APP_PASSWORD_ENV) or "").strip()
    if not username or not password:
        return None
    return {"server_url": ICLOUD_SERVER_URL,
            "username": username, "password": password}


def client_from_env(env: Optional[Mapping[str, str]] = None,
                    transport: Optional[CalDAVTransport] = None) -> CalDAVClient:
    """A client built from the env credentials; MissingCredentials when unset."""
    creds = icloud_credentials_from_env(env)
    if creds is None:
        raise MissingCredentials(
            f"{ICLOUD_USERNAME_ENV} and {ICLOUD_APP_PASSWORD_ENV} must both be set "
            "(the VM profile's .env; the app-specific password comes from "
            "appleid.apple.com → Sign-In and Security → App-Specific Passwords)")
    return CalDAVClient(creds["server_url"], creds["username"], creds["password"],
                         transport=transport)
