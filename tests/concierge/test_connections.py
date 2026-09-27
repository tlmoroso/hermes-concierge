"""Concierge account-connection contracts (Task 8.2): prerequisite probing,
dashboard deep-link building, and the copy builders behind the first-message
nudge + the ``/connect`` slash command.

The PURE decision layer is tested with STUBBED probes — no real provider
checks, no filesystem, no gateway imports (the discord_payloads.py
convention). The REAL probe (``account_probe``) is tested hermetically
against temp HERMES_HOME/XDG dirs with a stubbed ``shutil.which`` — it must
stay CHEAP-LOCAL (stat/which/env reads only, never network) because it runs
on the message path.

Discovery facts these tests pin (Task 8.1, recorded in the commit message):
- the gateway dashboard's real connect surfaces are SPA client routes
  (``/messaging`` = platform onboarding incl. the email mailbox account;
  ``/settings?tab=keys`` = the ENV & credentials page — the closest
  existing surface for the google-workspace skill's file-based OAuth);
- the dashboard's own origin comes from ``dashboard.public_url`` /
  ``HERMES_DASHBOARD_PUBLIC_URL`` via
  ``hermes_cli.dashboard_auth.prefix.resolve_public_url``;
- the ``hermes tools`` configured-probe is ``_toolset_has_keys``
  (hermes_cli/tools_config.py) — env/auth reads — but the concierge's
  account kinds aren't toolsets, so per-kind checks cover the surfaces the
  bots actually use (google_token.json for calendar, himalaya CLI+config
  for email)."""

import logging

import pytest

from concierge import connections
from concierge.blueprints import CATALOG


# ── missing_connections: which prerequisites are unconfigured ───────────────


def test_missing_connections_returns_exactly_the_unconfigured_kinds():
    scheduler = CATALOG["scheduler"]  # connects=("calendar",)
    assert connections.missing_connections(
        scheduler, probe=lambda kind: True) == []
    assert connections.missing_connections(
        scheduler, probe=lambda kind: False) == ["calendar"]


def test_missing_connections_empty_connects_means_empty(monkeypatch):
    """A blueprint with no connects never calls the probe — zero hot-path
    work for the reminders/researcher/notes bots."""
    calls = []

    def _spy(kind):
        calls.append(kind)
        return False

    assert connections.missing_connections(
        CATALOG["reminders"], probe=_spy) == []
    assert calls == []


def test_missing_connections_all_configured_is_empty():
    email_triage = CATALOG["email_triage"]  # connects=("email",)
    assert connections.missing_connections(
        email_triage, probe=lambda kind: True) == []


def test_missing_connections_dedupes_and_preserves_connects_order():
    class _Blueprint:
        connects = ("email", "calendar", "email")

    assert connections.missing_connections(
        _Blueprint(), probe=lambda kind: False) == ["email", "calendar"]


def test_missing_connections_accepts_a_plain_connects_tuple():
    assert connections.missing_connections(
        ("email",), probe=lambda kind: False) == ["email"]


def test_missing_connections_skips_non_string_entries_defensively():
    """A hand-edited catalog/registry must not crash the nudge decision."""
    assert connections.missing_connections(
        ("email", None, 7), probe=lambda kind: False) == ["email"]


# ── connect_links: deep-link payloads from the dashboard base URL ──────────


def test_connect_links_builds_label_and_url_from_the_real_paths():
    """The discovered page shapes: email onboards on the Messaging page,
    calendar lands on the Settings ENV & credentials page."""
    links = connections.connect_links(
        ["email", "calendar"], "https://gw.example.com")
    assert links == [
        {"kind": "email", "label": "Email",
         "url": "https://gw.example.com/messaging"},
        {"kind": "calendar", "label": "Calendar",
         "url": "https://gw.example.com/settings?tab=keys"},
    ]


def test_connect_links_strips_a_trailing_slash_from_the_base():
    links = connections.connect_links(
        ["email"], "https://gw.example.com/")
    assert links[0]["url"] == "https://gw.example.com/messaging"


def test_connect_links_blank_base_url_returns_no_links():
    """No configured dashboard origin → no link the user can tap; the
    renderers fall back to link-less copy (an un-tappable link is worse
    than none)."""
    assert connections.connect_links(["email"], "") == []
    assert connections.connect_links(["email"], None) == []


def test_connect_links_unmapped_kind_is_skipped_with_a_log(caplog):
    """UNKNOWN-KIND POLICY (picked + documented): a kind a blueprint needs
    but that has no dashboard path mapping is SKIPPED from link building
    with a logged reason — never an error (a missing mapping must not break
    the turn), never a guessed link."""
    with caplog.at_level(logging.DEBUG, logger="concierge.connections"):
        links = connections.connect_links(
            ["weather", "email"], "https://gw.example.com")
    assert [link["kind"] for link in links] == ["email"]
    assert any("weather" in rec.message for rec in caplog.records)


def test_connect_links_respects_an_injected_path_table():
    custom = {"email": {"label": "Mail", "path": "/mail"}}
    links = connections.connect_links(
        ["email"], "https://gw.example.com", kind_paths=custom)
    assert links == [{"kind": "email", "label": "Mail",
                      "url": "https://gw.example.com/mail"}]


# ── needed_kinds: what this guild's bound bots depend on ────────────────────


def test_needed_kinds_dedupes_across_bindings_in_first_seen_order():
    bindings = {
        "1": {"profile": "bot-scheduler-1", "blueprint": "scheduler",
              "guild_id": "42"},      # calendar
        "2": {"profile": "bot-email-2", "blueprint": "email_triage",
              "guild_id": "42"},      # email
        "3": {"profile": "bot-scheduler-3", "blueprint": "scheduler",
              "guild_id": "42"},      # calendar again (dedupe)
    }
    assert connections.needed_kinds(bindings, "42") == ["calendar", "email"]


def test_needed_kinds_is_guild_scoped_but_keeps_cli_bindings():
    bindings = {
        "1": {"profile": "bot-scheduler-1", "blueprint": "scheduler",
              "guild_id": "42"},
        "2": {"profile": "bot-scheduler-2", "blueprint": "scheduler",
              "guild_id": "other"},   # another server's bot
        "3": {"profile": "bot-email-3", "blueprint": "email_triage",
              "guild_id": None},      # CLI-provisioned: the operator's
    }
    assert connections.needed_kinds(bindings, "42") == ["calendar", "email"]


def test_needed_kinds_empty_for_connectless_blueprints():
    bindings = {"1": {"profile": "bot-reminders-1", "blueprint": "reminders",
                      "guild_id": "42"}}
    assert connections.needed_kinds(bindings, "42") == []


def test_needed_kinds_skips_unknown_blueprint_keys_defensively():
    bindings = {"1": {"profile": "bot-x", "blueprint": "nope",
                      "guild_id": "42"},
                "2": {"profile": "bot-scheduler-2", "blueprint": "scheduler",
                      "guild_id": "42"}}
    assert connections.needed_kinds(bindings, "42") == ["calendar"]


def test_probe_profile_for_kind_picks_the_first_guild_bot_needing_it():
    bindings = {
        "1": {"profile": "bot-reminders-1", "blueprint": "reminders",
              "guild_id": "42"},
        "2": {"profile": "bot-scheduler-2", "blueprint": "scheduler",
              "guild_id": "42"},
        "3": {"profile": "bot-scheduler-3", "blueprint": "scheduler",
              "guild_id": "other"},
    }
    assert connections.probe_profile_for(bindings, "42", "calendar") \
        == "bot-scheduler-2"
    assert connections.probe_profile_for(bindings, "42", "email") is None


# ── build_connect_status: the /connect no-arg listing ──────────────────────


def test_build_connect_status_lines_and_links():
    bindings = {
        "1": {"profile": "bot-scheduler-1", "blueprint": "scheduler",
              "guild_id": "42"},
        "2": {"profile": "bot-email-2", "blueprint": "email_triage",
              "guild_id": "42"},
    }
    payload = connections.build_connect_status(
        bindings, "42", configured={"calendar": False, "email": True},
        base_url="https://gw.example.com")
    assert payload["empty"] is False
    lines = "\n".join(payload["lines"])
    assert "Calendar" in lines and "not connected" in lines
    assert "Email" in lines and "connected" in lines
    # a deep link only for the UNCONFIGURED kind
    assert [link["kind"] for link in payload["links"]] == ["calendar"]
    assert payload["links"][0]["url"] == "https://gw.example.com/settings?tab=keys"


def test_build_connect_status_without_dashboard_base_has_no_links():
    bindings = {"1": {"profile": "bot-scheduler-1", "blueprint": "scheduler",
                      "guild_id": "42"}}
    payload = connections.build_connect_status(
        bindings, "42", configured={"calendar": False}, base_url="")
    assert payload["links"] == []
    assert "not connected" in payload["lines"][0]  # status still honest


def test_build_connect_status_empty_when_no_kinds_needed():
    bindings = {"1": {"profile": "bot-reminders-1", "blueprint": "reminders",
                      "guild_id": "42"}}
    payload = connections.build_connect_status(
        bindings, "42", configured={}, base_url="https://gw.example.com")
    assert payload["empty"] is True
    assert payload["lines"] == []
    assert payload["links"] == []


def test_build_connect_status_unmapped_kind_gets_a_status_line_no_link():
    class _Bp:
        connects = ("weather",)

    original = dict(connections.CATALOG)
    try:
        connections.CATALOG["weather_probe"] = _Bp()
        bindings = {"1": {"profile": "bot-w-1", "blueprint": "weather_probe",
                          "guild_id": "42"}}
        payload = connections.build_connect_status(
            bindings, "42", configured={"weather": False},
            base_url="https://gw.example.com")
        assert "Weather" in payload["lines"][0]
        assert payload["links"] == []
    finally:
        connections.CATALOG.clear()
        connections.CATALOG.update(original)


# ── build_connect_kind_reply: the /connect <kind> response ─────────────────


def test_kind_reply_unconfigured_carries_the_deep_link():
    payload = connections.build_connect_kind_reply(
        "email", configured=False, base_url="https://gw.example.com")
    assert payload["link"]["url"] == "https://gw.example.com/messaging"
    assert "Email" in payload["content"]


def test_kind_reply_configured_is_a_clean_nothing_to_do():
    payload = connections.build_connect_kind_reply(
        "calendar", configured=True, base_url="https://gw.example.com")
    assert payload["link"] is None
    assert "connected" in payload["content"]


def test_kind_reply_not_needed_still_offers_the_link():
    """``configured=None`` = no bot in this guild needs the kind — the user
    asked explicitly, so hand them the link without a status claim."""
    payload = connections.build_connect_kind_reply(
        "email", configured=None, base_url="https://gw.example.com")
    assert payload["link"]["url"].endswith("/messaging")


def test_kind_reply_without_dashboard_base_names_the_owner_step():
    payload = connections.build_connect_kind_reply(
        "calendar", configured=False, base_url="")
    assert payload["link"] is None
    assert "server owner" in payload["content"]


def test_kind_reply_unknown_kind_is_an_actionable_refusal():
    payload = connections.build_connect_kind_reply(
        "weather", configured=False, base_url="https://gw.example.com")
    assert payload["link"] is None
    assert "weather" in payload["content"]
    # the refusal names what CAN be linked
    assert "calendar" in payload["content"] and "email" in payload["content"]


# ── build_nudge_response: the first-message nudge copy ─────────────────────


def test_nudge_response_names_the_kind_and_carries_the_link():
    links = connections.connect_links(
        ["calendar"], "https://gw.example.com")
    payload = connections.build_nudge_response(["calendar"], links)
    text = payload["text"]
    assert "Calendar" in text
    assert "https://gw.example.com/settings?tab=keys" in text
    assert "connect" in text.lower()


def test_nudge_response_single_kind_with_link_pins_the_whole_sentence():
    """The M8 review defect, pinned WHOLE (not substrings): with a tappable
    link, the sentence must be coherent — the trailing "tap here to connect:"
    phrase CARRIES the link. The pre-fix shape spliced the bullet row into a
    sentence ("…your **Calendar** — [Connect](url) to do this — tap here to
    connect."), leaving the tappable link mid-sentence and the closing phrase
    dangling with nothing to tap."""
    links = connections.connect_links(
        ["calendar"], "https://gw.example.com")
    payload = connections.build_nudge_response(["calendar"], links)
    assert payload["text"] == (
        "I need access to your **Calendar** to do this — "
        "tap here to connect: "
        "[Connect Calendar](https://gw.example.com/settings?tab=keys)")


def test_nudge_response_without_links_still_names_the_kind_and_connect():
    """Blank dashboard base URL: the nudge must still fire — link-less copy
    points at /connect instead of a dead URL."""
    payload = connections.build_nudge_response(["calendar"], [])
    text = payload["text"]
    assert "Calendar" in text
    assert "/connect" in text
    assert "https://" not in text


def test_nudge_response_lists_every_missing_kind():
    links = connections.connect_links(
        ["calendar", "email"], "https://gw.example.com")
    payload = connections.build_nudge_response(["calendar", "email"], links)
    assert "Calendar" in payload["text"] and "Email" in payload["text"]
    assert "/messaging" in payload["text"]


# ── account_probe: the REAL probe (cheap local reads, hermetic tests) ───────


def test_account_probe_calendar_reads_the_bot_profile_token_file(
        tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    profile_dir = tmp_path / "profiles" / "bot-scheduler-1"
    profile_dir.mkdir(parents=True)
    probe = connections.account_probe("bot-scheduler-1")

    assert probe("calendar") is False  # no google_token.json yet
    (profile_dir / "google_token.json").write_text("{}")
    assert probe("calendar") is True


def test_account_probe_email_needs_himalaya_cli_and_config(
        tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    himalaya_dir = tmp_path / "xdg" / "himalaya"
    himalaya_dir.mkdir(parents=True)
    (himalaya_dir / "config.toml").write_text('imap = {}')
    probe = connections.account_probe("bot-email-1")

    monkeypatch.setattr("shutil.which", lambda name: None)
    assert probe("email") is False        # CLI missing
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/himalaya")
    assert probe("email") is True


def test_account_probe_unknown_kind_fails_open(monkeypatch, tmp_path):
    """No checker for the kind → read as CONFIGURED: the nudge must be
    certain before it nags (an unmapped kind must not produce a link-less
    nudge every binding)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert connections.account_probe("bot-x")("weather") is True


def test_account_probe_never_raises(monkeypatch, tmp_path):
    """A probe failure (bad profile name, unwritable fs) reads as configured
    — error-isolated by construction, never a crash on the message path."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    probe = connections.account_probe("")
    assert probe("calendar") is True


def test_dashboard_base_url_reads_the_canonical_resolver(monkeypatch):
    """The base URL is the dashboard's OWN public-URL config (Task 8.1b):
    ``resolve_public_url`` (env HERMES_DASHBOARD_PUBLIC_URL → config
    dashboard.public_url). The helper only wraps it; a resolver failure
    must degrade to "" (no links), never raise."""
    monkeypatch.setattr(
        "hermes_cli.dashboard_auth.prefix.resolve_public_url",
        lambda: "https://gw.example.com")
    assert connections.dashboard_base_url() == "https://gw.example.com"
    monkeypatch.setattr(
        "hermes_cli.dashboard_auth.prefix.resolve_public_url",
        lambda: "")
    assert connections.dashboard_base_url() == ""
