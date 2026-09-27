"""Tests for hermes_cli/webhook.py — webhook subscription CLI."""

import json
import os
import pytest
import stat
from argparse import Namespace

from hermes_cli.webhook import (
    webhook_command,
    _get_webhook_base_url,
    _load_subscriptions,
    _save_subscriptions,
    _subscriptions_path,
)

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Default: webhooks enabled (most tests need this)
    monkeypatch.setattr(
        "hermes_cli.webhook._is_webhook_enabled", lambda: True
    )

def _make_args(**kwargs):
    defaults = {
        "webhook_action": None,
        "name": "",
        "prompt": "",
        "events": "",
        "description": "",
        "skills": "",
        "deliver": "log",
        "deliver_chat_id": "",
        "secret": "",
        "route_profile": None,
        "payload": "",
        "script": "",
        "handler": "",
    }
    defaults.update(kwargs)
    return Namespace(**defaults)

@pytest.mark.parametrize("host", [None, "", "0.0.0.0", "::"])
def test_webhook_base_url_maps_wildcard_hosts_to_localhost(monkeypatch, host):
    monkeypatch.setattr(
        "hermes_cli.webhook._get_webhook_config",
        lambda: {"extra": {"host": host, "port": 9123}},
    )
    assert _get_webhook_base_url() == "http://localhost:9123"

class TestSubscribe:

    def test_custom_secret(self):
        webhook_command(_make_args(
            webhook_action="subscribe", name="s", secret="my-secret"
        ))
        assert _load_subscriptions()["s"]["secret"] == "my-secret"

    def test_auto_secret(self):
        webhook_command(_make_args(webhook_action="subscribe", name="s"))
        secret = _load_subscriptions()["s"]["secret"]
        assert len(secret) > 20

    def test_profile_binding_and_secret_survive_update(self, tmp_path, capsys):
        profile_dir = tmp_path / "profiles" / "compta"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("{}\n")  # identity marker

        webhook_command(_make_args(
            webhook_action="subscribe", name="notifier", route_profile="compta"
        ))
        created = _load_subscriptions()["notifier"]
        first_secret = created["secret"]
        assert created["profile"] == "compta"
        assert "/p/compta/webhooks/notifier" in capsys.readouterr().out

        webhook_command(_make_args(
            webhook_action="subscribe", name="notifier", description="updated"
        ))
        updated = _load_subscriptions()["notifier"]
        assert updated["profile"] == "compta"
        assert updated["secret"] == first_secret

    def test_rejects_unknown_profile_without_replacing_subscription(self, capsys):
        webhook_command(_make_args(
            webhook_action="subscribe", name="notifier", secret="original"
        ))
        webhook_command(_make_args(
            webhook_action="subscribe", name="notifier", route_profile="missing"
        ))

        assert _load_subscriptions()["notifier"]["secret"] == "original"

class TestCronJobSubscribe:
    """--cron-job: event-triggered cron jobs."""

    def test_valid_job_ref_stored_as_id(self, monkeypatch):
        # resolve_job_ref is imported inside _cmd_subscribe from cron.jobs
        import cron.jobs as jobs_mod

        monkeypatch.setattr(
            jobs_mod, "resolve_job_ref",
            lambda ref: {"id": "job-abc123", "name": ref},
        )
        webhook_command(_make_args(
            webhook_action="subscribe", name="ev", cron_job="sweeper"
        ))
        assert _load_subscriptions()["ev"]["cron_job"] == "job-abc123"

    def test_unknown_job_rejected(self, monkeypatch, capsys):
        import cron.jobs as jobs_mod

        monkeypatch.setattr(jobs_mod, "resolve_job_ref", lambda ref: None)
        webhook_command(_make_args(
            webhook_action="subscribe", name="ev", cron_job="nope"
        ))
        assert "ev" not in _load_subscriptions()

    def test_cron_job_plus_deliver_only_rejected(self, capsys):
        webhook_command(_make_args(
            webhook_action="subscribe",
            name="ev",
            cron_job="sweeper",
            deliver_only=True,
            deliver="telegram",
        ))
        assert "ev" not in _load_subscriptions()


class TestHandlerRoutes:
    """--handler: code-backed route kinds (Task 10.1's concierge-location)."""

    def test_handler_stored_on_route(self, capsys):
        webhook_command(_make_args(
            webhook_action="subscribe", name="concierge-location",
            handler="concierge-location"))
        route = _load_subscriptions()["concierge-location"]
        assert route["handler"] == "concierge-location"
        assert "handler 'concierge-location'" in capsys.readouterr().out

    def test_unknown_handler_rejected(self, capsys):
        webhook_command(_make_args(
            webhook_action="subscribe", name="x", handler="does-not-exist"))
        assert "Unknown handler" in capsys.readouterr().out
        assert "x" not in _load_subscriptions()

    def test_handler_plus_cron_job_rejected(self, monkeypatch, capsys):
        import cron.jobs as jobs_mod

        monkeypatch.setattr(
            jobs_mod, "resolve_job_ref",
            lambda ref: {"id": "job-abc123", "name": ref},
        )
        webhook_command(_make_args(
            webhook_action="subscribe", name="x",
            handler="concierge-location", cron_job="sweeper"))
        assert "mutually exclusive" in capsys.readouterr().out
        assert "x" not in _load_subscriptions()

    def test_handler_plus_deliver_only_rejected(self, capsys):
        webhook_command(_make_args(
            webhook_action="subscribe", name="x",
            handler="concierge-location",
            deliver_only=True, deliver="discord"))
        assert "mutually exclusive" in capsys.readouterr().out
        assert "x" not in _load_subscriptions()

    def test_handler_survives_update_and_lists(self, capsys):
        webhook_command(_make_args(
            webhook_action="subscribe", name="loc", handler="concierge-location"))
        # an update without --handler keeps the stored kind (same as profile/secret)
        webhook_command(_make_args(webhook_action="subscribe", name="loc"))
        assert _load_subscriptions()["loc"]["handler"] == "concierge-location"
        webhook_command(_make_args(webhook_action="list"))
        assert "handler 'concierge-location'" in capsys.readouterr().out


class TestListSecretRedaction:
    """Kept from the concierge branch across main's lane-py14 purge
    (8f6109e): the secret-never-printed invariant is a security property,
    not a low-value test. The purged TestList.test_with_entries stays purged."""

    def test_list_never_prints_the_route_secret(self, capsys):
        """M10 (doc correction): the route token is shown ONLY by
        `hermes webhook subscribe` at creation — `list` prints
        URL/Profile/Events/Deliver (and Script) and never the Secret."""
        webhook_command(_make_args(
            webhook_action="subscribe", name="loc", secret="tok-DO-NOT-PRINT"))
        assert "tok-DO-NOT-PRINT" in capsys.readouterr().out  # subscribe does
        webhook_command(_make_args(webhook_action="list"))
        out = capsys.readouterr().out
        assert "tok-DO-NOT-PRINT" not in out
        assert "Secret" not in out


class TestRemove:

    def test_selective_remove(self):
        webhook_command(_make_args(webhook_action="subscribe", name="keep"))
        webhook_command(_make_args(webhook_action="subscribe", name="drop"))
        webhook_command(_make_args(webhook_action="remove", name="drop"))
        subs = _load_subscriptions()
        assert "keep" in subs
        assert "drop" not in subs

class TestPersistence:

    def test_corrupted_file(self):
        path = _subscriptions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("broken{{{")
        assert _load_subscriptions() == {}

    @pytest.mark.platforms("posix")  # POSIX mode bits are platform-specific
    def test_save_creates_secret_file_owner_only_under_permissive_umask(self):
        old_umask = os.umask(0o022)
        try:
            _save_subscriptions({"demo": {"secret": "TOPSECRET", "prompt": "x"}})
        finally:
            os.umask(old_umask)

        path = _subscriptions_path()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert "TOPSECRET" in path.read_text(encoding="utf-8")

    @pytest.mark.platforms("posix")  # POSIX mode bits are platform-specific
    def test_save_narrows_existing_broad_secret_file_mode(self):
        # Simulate a pre-existing 0o644 file from before this hardening landed.
        path = _subscriptions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"old": {"secret": "stale", "prompt": "x"}}))
        path.chmod(0o644)

        _save_subscriptions({"demo": {"secret": "FRESH", "prompt": "x"}})

        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert "FRESH" in path.read_text(encoding="utf-8")

class TestWebhookEnabledGate:

    def test_blocks_list_when_disabled(self, capsys, monkeypatch):
        monkeypatch.setattr("hermes_cli.webhook._is_webhook_enabled", lambda: False)
        webhook_command(_make_args(webhook_action="list"))
        out = capsys.readouterr().out
        assert "not enabled" in out.lower()
