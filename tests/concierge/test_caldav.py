"""CalDAV reader tests (Task 10.3, APPLE_BRIDGE option (a)) — fixtures only, no network.

The repo has NO CalDAV library (pyproject.toml/uv.lock carry neither ``caldav`` nor
``icalendar``), so concierge/caldav.py is a minimal RFC 5545/4791 client with a
pluggable transport. These tests pin the parsing/formatting logic against VCALENDAR
fixtures and drive the client through a fake transport — no request ever leaves the
process.
"""

import base64
from datetime import date, datetime, timezone, UTC

import pytest

from concierge import caldav


# ---------------------------------------------------------------------------
# Fixtures (VCalendar strings — the shapes iCloud returns)
# ---------------------------------------------------------------------------

EVENTS_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Apple Inc.//iOS 20.0//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:1111-2222\r\n"
    "SUMMARY:Standup\r\n"
    "DTSTART;TZID=America/Los_Angeles:20260921T090000\r\n"
    "DTEND:20260921T163000Z\r\n"
    "LOCATION:Room 4\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:3333-4444\r\n"
    "SUMMARY:All-day offsite\r\n"
    "DTSTART;VALUE=DATE:20260921\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)

FOLDED_SUMMARY_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:fold-1\r\n"
    "SUMMARY:a summary Apple fol\r\n"
    " ded over two lines\r\n"
    "DTSTART:20260921T090000Z\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)

TODOS_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "BEGIN:VTODO\r\n"
    "UID:todo-open\r\n"
    "SUMMARY:Water the plants\r\n"
    "DUE;TZID=America/Los_Angeles:20260922T090000\r\n"
    "STATUS:NEEDS-ACTION\r\n"
    "DESCRIPTION:ferns only\r\n"
    "END:VTODO\r\n"
    "BEGIN:VTODO\r\n"
    "UID:todo-done\r\n"
    "SUMMARY:Book dentist\\, semi-annual\r\n"
    "COMPLETED:20260920T120000Z\r\n"
    "END:VTODO\r\n"
    "BEGIN:VTODO\r\n"
    "UID:todo-status-done\r\n"
    "SUMMARY:File expenses\r\n"
    "STATUS:COMPLETED\r\n"
    "END:VTODO\r\n"
    "END:VCALENDAR\r\n"
)

# A VTODO carrying a VALARM whose DESCRIPTION differs from the VTODO's own —
# the M10 reviewer's reproduction: without sub-component depth tracking the
# VALARM's "Reminder" clobbered the real notes ("ferns only").
VALARM_TODOS_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "BEGIN:VTODO\r\n"
    "UID:todo-alarm\r\n"
    "SUMMARY:Water the plants\r\n"
    "DUE;TZID=America/Los_Angeles:20260922T090000\r\n"
    "DESCRIPTION:ferns only\r\n"
    "BEGIN:VALARM\r\n"
    "ACTION:DISPLAY\r\n"
    "DESCRIPTION:Reminder\r\n"
    "TRIGGER:-PT10M\r\n"
    "END:VALARM\r\n"
    "END:VTODO\r\n"
    "END:VCALENDAR\r\n"
)

# An all-day reminder: DUE;VALUE=DATE (no time part) — the params are
# discarded by _components, so the no-'T' value heuristic must carry it.
ALL_DAY_TODO_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "BEGIN:VTODO\r\n"
    "UID:todo-allday\r\n"
    "SUMMARY:Trash day\r\n"
    "DUE;VALUE=DATE:20260925\r\n"
    "END:VTODO\r\n"
    "END:VCALENDAR\r\n"
)


def _multistatus(*calendar_bodies: str) -> str:
    responses = "".join(
        "<D:response><D:href>/{i}.ics</D:href><D:propstat><D:prop>"
        "<C:calendar-data>{body}</C:calendar-data></D:prop></D:propstat>"
        "</D:response>".format(i=i, body=body.replace("&", "&amp;").replace("<", "&lt;"))
        for i, body in enumerate(calendar_bodies))
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<D:multistatus xmlns:D="DAV:" '
            'xmlns:C="urn:ietf:params:xml:ns:caldav">' + responses + "</D:multistatus>")


class FakeTransport:
    """Records every request; replays canned responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, *, body=None, headers=None):
        self.requests.append(
            {"method": method, "url": url, "body": body,
             "headers": dict(headers or {})})
        response = self.responses.pop(0)
        return caldav.TransportResponse(status=response[0], body=response[1])


# ---------------------------------------------------------------------------
# VEVENT parsing
# ---------------------------------------------------------------------------

class TestParseEvents:
    def test_parses_timed_and_all_day_events(self):
        events = caldav.parse_events(EVENTS_ICS)
        assert len(events) == 2
        standup, offsite = events
        assert standup.uid == "1111-2222"
        assert standup.summary == "Standup"
        assert standup.location == "Room 4"
        assert standup.start == datetime(2026, 9, 21, 9, 0)
        assert standup.end == datetime(2026, 9, 21, 16, 30, tzinfo=UTC)
        assert offsite.start == date(2026, 9, 21)  # VALUE=DATE
        assert offsite.end is None  # no DTEND in the fixture

    def test_unfolds_continuation_lines(self):
        events = caldav.parse_events(FOLDED_SUMMARY_ICS)
        assert events[0].summary == "a summary Apple folded over two lines"

    def test_garbage_is_empty_not_a_crash(self):
        assert caldav.parse_events("") == []
        assert caldav.parse_events("not ical at all") == []
        assert caldav.parse_events("BEGIN:VEVENT\r\nSUMMARY:no end") == []

    def test_lf_only_line_endings_accepted(self):
        events = caldav.parse_events(EVENTS_ICS.replace("\r\n", "\n"))
        assert len(events) == 2


# ---------------------------------------------------------------------------
# VTODO parsing
# ---------------------------------------------------------------------------

class TestParseTodos:
    def test_parses_open_todo_with_due_and_notes(self):
        todos = caldav.parse_todos(TODOS_ICS)
        open_todo = next(t for t in todos if t.uid == "todo-open")
        assert open_todo.summary == "Water the plants"
        assert open_todo.due == datetime(2026, 9, 22, 9, 0)
        assert open_todo.completed is None
        assert open_todo.notes == "ferns only"

    def test_completed_property_marks_done(self):
        todos = caldav.parse_todos(TODOS_ICS)
        done = next(t for t in todos if t.uid == "todo-done")
        assert done.completed == datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
        assert done.summary == "Book dentist, semi-annual"  # unescaped \,

    def test_status_completed_marks_done(self):
        todos = caldav.parse_todos(TODOS_ICS)
        status_done = next(t for t in todos if t.uid == "todo-status-done")
        assert status_done.completed is None  # no COMPLETED stamp
        assert status_done.status == "COMPLETED"

    def test_garbage_is_empty_not_a_crash(self):
        assert caldav.parse_todos("404 page") == []

    def test_all_day_value_date_due_parses_to_date(self):
        """VALUE=DATE (all-day) DUE: the property params never reach
        _parse_ical_dt (_components discards them — the M10 dead-branch
        removal), so the no-'T' value heuristic is the discriminator. Verified
        by execution before the branch was removed; this pins it."""
        todos = caldav.parse_todos(ALL_DAY_TODO_ICS)
        assert todos[0].due == date(2026, 9, 25)


# ---------------------------------------------------------------------------
# Nested components (VALARM) — M10: sub-component lines must not leak
# ---------------------------------------------------------------------------

class TestNestedComponents:
    """Lines inside a nested component (a VTODO's VALARM) belong to the child,
    not the parent. Before the depth-tracking fix, last-write-wins leaked them
    in: the reviewer's repro read a VTODO's notes as 'Reminder' (the VALARM's
    DESCRIPTION) instead of 'ferns only'."""

    def test_valarm_description_does_not_clobber_vtodo_notes(self):
        todos = caldav.parse_todos(VALARM_TODOS_ICS)
        assert len(todos) == 1
        assert todos[0].notes == "ferns only"  # the VTODO's own DESCRIPTION…
        assert todos[0].notes != "Reminder"    # …not the VALARM's

    def test_valarm_lines_do_not_leak_into_the_parent_block(self):
        blocks = caldav._components(caldav._unfold(VALARM_TODOS_ICS), "VTODO")
        assert blocks == [{"UID": "todo-alarm",
                           "SUMMARY": "Water the plants",
                           "DUE": "20260922T090000",
                           "DESCRIPTION": "ferns only"}]  # no ACTION/TRIGGER/BEGIN

    def test_parent_properties_survive_a_valarm(self):
        todos = caldav.parse_todos(VALARM_TODOS_ICS)
        assert todos[0].summary == "Water the plants"
        assert todos[0].due == datetime(2026, 9, 22, 9, 0)

    def test_depth_tracking_past_one_level(self):
        """A sub-component nested inside the sub-component (hand-edited or
        exotic producers): the depth counter must not stop at one."""
        ics = ("BEGIN:VCALENDAR\r\n"
               "BEGIN:VTODO\r\n"
               "SUMMARY:parent\r\n"
               "DESCRIPTION:parent notes\r\n"
               "BEGIN:VALARM\r\n"
               "DESCRIPTION:alarm\r\n"
               "BEGIN:SUB\r\n"
               "DESCRIPTION:deeper\r\n"
               "END:SUB\r\n"
               "END:VALARM\r\n"
               "END:VTODO\r\n"
               "END:VCALENDAR\r\n")
        todos = caldav.parse_todos(ics)
        assert todos[0].notes == "parent notes"
        assert todos[0].summary == "parent"

    def test_nested_components_do_not_break_sibling_blocks(self):
        """A VTODO with a VALARM followed by a plain VTODO: both parse, in
        order, and the second is untouched by the first's nesting."""
        ics = (VALARM_TODOS_ICS.replace("END:VCALENDAR\r\n", "")
               + "BEGIN:VTODO\r\n"
                 "UID:todo-plain\r\n"
                 "SUMMARY:Plain\r\n"
                 "END:VTODO\r\n"
                 "END:VCALENDAR\r\n")
        todos = caldav.parse_todos(ics)
        assert [t.uid for t in todos] == ["todo-alarm", "todo-plain"]
        assert todos[1].summary == "Plain"


# ---------------------------------------------------------------------------
# Datetime parsing contract (post dead-branch removal: value-only)
# ---------------------------------------------------------------------------

class TestParseIcalDt:
    def test_date_only_z_floating_and_garbage(self):
        """The M10 simplification: no params argument — the value alone
        discriminates. Date-only (what a VALUE=DATE property carries once
        _components strips its params) has no 'T' and parses to a date."""
        assert caldav._parse_ical_dt("20260921") == date(2026, 9, 21)
        assert caldav._parse_ical_dt(" 20260921 ") == date(2026, 9, 21)  # stripped
        assert caldav._parse_ical_dt("20260921T090000Z") == datetime(
            2026, 9, 21, 9, 0, tzinfo=UTC)
        assert caldav._parse_ical_dt("20260921T090000") == datetime(2026, 9, 21, 9, 0)
        assert caldav._parse_ical_dt("not-a-date") is None
        assert caldav._parse_ical_dt("") is None
        assert caldav._parse_ical_dt("2026") is None  # too short for a date


# ---------------------------------------------------------------------------
# Reminder creation payload formatting
# ---------------------------------------------------------------------------

class TestBuildReminderVtodo:
    def test_minimal_reminder_round_trips_through_parse(self):
        body = caldav.build_reminder_vtodo("Call the bank")
        todos = caldav.parse_todos(body)
        assert len(todos) == 1
        assert todos[0].summary == "Call the bank"
        assert todos[0].completed is None
        assert todos[0].due is None

    def test_reminder_with_due_and_notes_round_trips(self):
        due = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
        body = caldav.build_reminder_vtodo(
            "Pay rent; late after the 3rd, seriously", due=due, notes="line one\nline two")
        todos = caldav.parse_todos(body)
        todo = todos[0]
        assert todo.summary == "Pay rent; late after the 3rd, seriously"
        assert todo.notes == "line one\nline two"
        assert todo.due == due

    def test_rfc5545_text_escaping(self):
        assert caldav.escape_text("a;b,c\nd\\e") == "a\\;b\\,c\\nd\\\\e"
        assert caldav.escape_text("plain") == "plain"

    def test_uid_and_dtstamp_present_and_unique(self):
        first = caldav.build_reminder_vtodo("x")
        second = caldav.build_reminder_vtodo("x")
        uids = {caldav.parse_todos(b)[0].uid for b in (first, second)}
        assert len(uids) == 2  # two calls, two UIDs — never collide server-side
        for body in (first, second):
            assert "DTSTAMP:" in body
            assert "PRODID:" in body and "BEGIN:VTODO" in body

    def test_long_lines_are_folded_to_75_octets(self):
        body = caldav.build_reminder_vtodo("summary " * 40)
        longest = max(len(line.encode("utf-8")) for line in body.splitlines())
        assert longest <= 75

    def test_explicit_dtstamp_and_uid_are_honored(self):
        body = caldav.build_reminder_vtodo(
            "x", uid="fixed-uid", dtstamp=datetime(2026, 9, 21, 12, 0, tzinfo=UTC))
        assert "UID:fixed-uid" in body
        assert "DTSTAMP:20260921T120000Z" in body


# ---------------------------------------------------------------------------
# CalDAV REPORT query formatting
# ---------------------------------------------------------------------------

class TestQueryBodies:
    def test_events_time_range_query(self):
        start = datetime(2026, 9, 21, tzinfo=UTC)
        end = datetime(2026, 9, 22, tzinfo=UTC)
        body = caldav.build_events_time_range_query(start, end)
        assert 'start="20260921T000000Z"' in body
        assert 'end="20260922T000000Z"' in body
        assert 'name="VEVENT"' in body
        assert "calendar-query" in body

    def test_incomplete_todos_query(self):
        body = caldav.build_incomplete_todos_query()
        assert 'name="VTODO"' in body
        assert "<C:prop-filter name=\"COMPLETED\">" in body
        assert "<C:is-not-defined />" in body


# ---------------------------------------------------------------------------
# Auth from env — never hardcoded
# ---------------------------------------------------------------------------

class TestCredentialsFromEnv:
    def test_reads_app_specific_password_pair(self, monkeypatch):
        monkeypatch.setenv("ICLOUD_USERNAME", "user@example.com")
        monkeypatch.setenv("ICLOUD_APP_PASSWORD", "abcd-efgh-ijkl-mnop")
        creds = caldav.icloud_credentials_from_env()
        assert creds == {
            "server_url": "https://caldav.icloud.com",
            "username": "user@example.com",
            "password": "abcd-efgh-ijkl-mnop"}

    def test_missing_either_var_is_none(self, monkeypatch):
        monkeypatch.delenv("ICLOUD_USERNAME", raising=False)
        monkeypatch.delenv("ICLOUD_APP_PASSWORD", raising=False)
        assert caldav.icloud_credentials_from_env() is None
        monkeypatch.setenv("ICLOUD_USERNAME", "user@example.com")
        assert caldav.icloud_credentials_from_env() is None  # password still missing


# ---------------------------------------------------------------------------
# Client wiring through the pluggable transport (no network)
# ---------------------------------------------------------------------------

class TestCalDAVClient:
    def _client(self, responses):
        transport = FakeTransport(responses)
        client = caldav.CalDAVClient(
            "https://caldav.icloud.com/", "user@example.com", "app-password",
            transport=transport)
        return client, transport

    def test_list_today_events_reports_and_parses(self):
        client, transport = self._client(
            [(207, _multistatus(EVENTS_ICS, FOLDED_SUMMARY_ICS))])
        today = datetime(2026, 9, 21, tzinfo=UTC)
        events = client.list_today_events("/user/calendars/work/", today=today)
        report = transport.requests[0]
        assert report["method"] == "REPORT"
        assert report["url"] == "https://caldav.icloud.com/user/calendars/work/"
        assert 'start="20260921T000000Z"' in report["body"]
        assert report["headers"]["Depth"] == "1"
        assert len(events) == 3  # both responses parsed
        assert events[0].summary == "Standup"

    def test_list_incomplete_reminders_filters_completed(self):
        client, _ = self._client([(207, _multistatus(TODOS_ICS))])
        reminders = client.list_incomplete_reminders("/user/reminders/")
        assert [r.summary for r in reminders] == ["Water the plants"]

    def test_create_reminder_posts_vtodo(self):
        client, transport = self._client([(201, "")])
        due = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
        ok = client.create_reminder(
            "/user/reminders/", "Submit report", due=due, notes="by Friday")
        assert ok is True
        post = transport.requests[0]
        assert post["method"] == "POST"
        assert post["url"] == "https://caldav.icloud.com/user/reminders/"
        assert post["headers"]["Content-Type"] == "text/calendar; charset=utf-8"
        assert "SUMMARY:Submit report" in post["body"]
        assert "DUE:20260922T090000Z" in post["body"]

    def test_create_reminder_failure_is_false(self):
        client, _ = self._client([(403, "Forbidden")])
        assert client.create_reminder("/user/reminders/", "x") is False

    def test_default_transport_sends_basic_auth(self):
        """The production transport carries the credential as Basic auth."""
        transport = caldav.RequestsTransport("user@example.com", "app-password")
        expected = base64.b64encode(b"user@example.com:app-password").decode()
        headers = transport._headers()
        assert headers["Authorization"] == f"Basic {expected}"
        assert headers["Content-Type"] == "text/calendar; charset=utf-8"

    def test_client_from_env_credentials(self, monkeypatch):
        monkeypatch.setenv("ICLOUD_USERNAME", "user@example.com")
        monkeypatch.setenv("ICLOUD_APP_PASSWORD", "app-password")
        client = caldav.client_from_env(transport=FakeTransport([]))
        assert client.server_url == "https://caldav.icloud.com"
        assert client.username == "user@example.com"
        assert client.password == "app-password"

    def test_client_from_env_without_credentials_raises(self, monkeypatch):
        monkeypatch.delenv("ICLOUD_USERNAME", raising=False)
        monkeypatch.delenv("ICLOUD_APP_PASSWORD", raising=False)
        with pytest.raises(caldav.MissingCredentials):
            caldav.client_from_env(transport=FakeTransport([]))
