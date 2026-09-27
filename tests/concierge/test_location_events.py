"""Location events (Task 10.1): phone region enter/exit -> reminder-bot run target.

``handle_location_event`` is the plan's pure API: payload in, prompt (or None)
out. ``resolve_location_run`` is the richer seam the webhook route kind uses —
it returns the prompt AND the bound bot profile + channel, so the gateway can
run the turn AS the bot and deliver to the channel (the same origin delivery
the bot's cron routine uses).

Contracts:
- A valid enter/exit payload against a bound channel returns a prompt naming
  the region and the event kind (arrived/left).
- No ``concierge.location_channel_id`` configured -> None (logged, never raised).
- Channel configured but unbound (or bound to a ghost/retired profile) -> None.
- Garbage payloads (non-dict, missing fields, wrong types, unknown event
  values) -> None with a logged reason — a webhook POST can never crash the
  gateway through this path.
"""

import logging

import pytest

from concierge import registry
from concierge.location_events import handle_location_event, resolve_location_run


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Temp HERMES_HOME with a live reminder-bot profile dir and one binding."""
    root = tmp_path
    (root / "profiles" / "bot-reminders-a1b2").mkdir(parents=True)
    (root / "profiles" / "bot-reminders-a1b2" / ".env").write_text("K=1\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    registry.bind_channel("100", profile="bot-reminders-a1b2",
                          blueprint="reminders", guild_id="42")
    return root


def _cfg(channel_id="100"):
    return {"concierge": {"location_channel_id": channel_id}}


# ── the plan's API: payload in, prompt out ───────────────────────────────────


def test_enter_event_returns_prompt_naming_region_and_arrival(home):
    prompt = handle_location_event(
        {"user": "tim", "region": "work", "event": "enter"}, cfg=_cfg())
    assert prompt is not None
    assert "arrived at work" in prompt
    assert "work" in prompt  # region named for the reminder lookup
    assert "briefly" in prompt  # the phone-first reply contract


def test_exit_event_returns_prompt_naming_region_and_leaving(home):
    prompt = handle_location_event(
        {"user": "tim", "region": "gym", "event": "exit"}, cfg=_cfg())
    assert prompt is not None
    assert "left gym" in prompt


def test_unset_channel_returns_none(home, caplog):
    with caplog.at_level(logging.DEBUG, logger="concierge.location_events"):
        assert handle_location_event(
            {"user": "tim", "region": "work", "event": "enter"},
            cfg={"concierge": {}}) is None
    assert "location_channel_id" in caplog.text


def test_unbound_channel_returns_none(home, caplog):
    with caplog.at_level(logging.DEBUG, logger="concierge.location_events"):
        assert handle_location_event(
            {"user": "tim", "region": "work", "event": "enter"},
            cfg=_cfg(channel_id="999")) is None
    assert "999" in caplog.text


def test_ghost_profile_binding_returns_none(home, caplog):
    """A binding whose profile dir is gone (bot retired under us) never runs:
    the run would fire into a scope that does not exist."""
    registry.bind_channel("100", profile="bot-ghost-zz9zz",
                          blueprint="reminders", guild_id="42")
    with caplog.at_level(logging.DEBUG, logger="concierge.location_events"):
        assert handle_location_event(
            {"user": "tim", "region": "work", "event": "enter"},
            cfg=_cfg()) is None
    assert "bot-ghost-zz9zz" in caplog.text


# ── payload validation: garbage never raises, never runs ─────────────────────


@pytest.mark.parametrize("payload", [
    None,
    "not a dict",
    42,
    [],                                    # wrong container
    {},                                    # every field missing
    {"region": "work", "event": "enter"},  # user missing
    {"user": "tim", "event": "enter"},     # region missing
    {"user": "tim", "region": "work"},     # event missing
    {"user": "tim", "region": "work", "event": "teleported"},  # unknown event
    {"user": "tim", "region": "work", "event": "ENTER"},       # case-sensitive
    {"user": "", "region": "work", "event": "enter"},          # empty user
    {"user": "tim", "region": "", "event": "enter"},           # empty region
    {"user": 7, "region": "work", "event": "enter"},           # wrong types
    {"user": "tim", "region": ["work"], "event": "enter"},
    {"user": "tim", "region": "w" * 500, "event": "enter"},    # garbage region
])
def test_garbage_payloads_return_none_without_raising(home, payload, caplog):
    with caplog.at_level(logging.DEBUG, logger="concierge.location_events"):
        assert handle_location_event(payload, cfg=_cfg()) is None
    assert caplog.text.strip()  # a reason was logged


def test_surrounding_whitespace_is_tolerated(home):
    """Shortcuts users type the region name; stray spaces are not garbage."""
    prompt = handle_location_event(
        {"user": " tim ", "region": " work ", "event": "enter"}, cfg=_cfg())
    assert prompt is not None
    assert "arrived at work" in prompt


# ── the webhook seam: prompt + profile + channel in one verdict ──────────────


def test_resolve_location_run_returns_profile_and_channel(home):
    run = resolve_location_run(
        {"user": "tim", "region": "work", "event": "enter"}, cfg=_cfg())
    assert run is not None
    assert run["profile"] == "bot-reminders-a1b2"
    assert run["channel_id"] == "100"
    assert run["region"] == "work"
    assert run["event"] == "enter"
    assert "arrived at work" in run["prompt"]


def test_resolve_location_run_no_target_is_none(home):
    assert resolve_location_run(
        {"user": "tim", "region": "work", "event": "enter"},
        cfg={"concierge": {}}) is None


def test_handle_location_event_loads_config_when_cfg_is_none(home):
    """The plan's bare signature (``handle_location_event(payload)``) reads
    config.yaml from the active home — defaults-only when unreadable."""
    (home / "config.yaml").write_text(
        "concierge:\n  location_channel_id: '100'\n", encoding="utf-8")
    prompt = handle_location_event(
        {"user": "tim", "region": "work", "event": "enter"})
    assert prompt is not None
    assert "arrived at work" in prompt


def test_handle_location_event_defaults_when_config_missing(home):
    """No config.yaml at all: defaults-only -> no channel -> None, no raise."""
    assert handle_location_event(
        {"user": "tim", "region": "work", "event": "enter"}) is None


# ── M10: the registry read may fail unreadable — ignored, never a raise ─────


def test_unreadable_registry_is_none_never_a_raise(tmp_path, monkeypatch, caplog):
    """A registry that exists but cannot be read (EACCES/EIO/stalled mount —
    staged here as a directory clobbering the file, which raises OSError on
    every platform and user, root included) is the M5b unreadable-registry
    posture: warn once inside load_registry, then the hub/ignored fallback.
    M10: the OSError used to escape resolve_location_run/handle_location_event
    (their 'never raises' contract) and 500 the gateway route."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reg = registry.registry_path()
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.mkdir()  # exists, unreadable as a JSON file
    payload = {"user": "tim", "region": "work", "event": "enter"}
    cfg = {"concierge": {"location_channel_id": "100"}}
    with caplog.at_level(logging.DEBUG, logger="concierge.location_events"):
        assert handle_location_event(payload, cfg=cfg) is None
        assert resolve_location_run(payload, cfg=cfg) is None
    assert "registry unreadable" in caplog.text


# ── M10: invalid-payload debug logs are capped, not a 1MB mirror ────────────


def test_invalid_payload_logs_a_truncated_repr(home, caplog):
    """The webhook body cap is 1MB; the 'missing or invalid user/region'
    reason logs must not mirror an attacker-sized payload wholesale — first
    200 chars of the repr + '...' (``user`` itself stays uncapped: it is
    unused downstream)."""
    payload = {"user": "", "region": "work", "event": "enter",
               "blob": "x" * 5000}
    with caplog.at_level(logging.DEBUG, logger="concierge.location_events"):
        assert handle_location_event(payload, cfg=_cfg()) is None
    msg = next(r.getMessage() for r in caplog.records
               if "invalid 'user'" in r.getMessage())
    # ~50-char prefix + 200-char repr cap + '...' — never the 5KB payload
    assert msg.endswith("...") and len(msg) <= 255
    assert "x" * 300 not in msg


def test_invalid_region_logs_a_truncated_repr(home, caplog):
    payload = {"user": "tim", "region": None, "event": "enter",
               "blob": "y" * 5000}
    with caplog.at_level(logging.DEBUG, logger="concierge.location_events"):
        assert handle_location_event(payload, cfg=_cfg()) is None
    msg = next(r.getMessage() for r in caplog.records
               if "invalid 'region'" in r.getMessage())
    assert msg.endswith("...") and len(msg) <= 255
    assert "y" * 300 not in msg
