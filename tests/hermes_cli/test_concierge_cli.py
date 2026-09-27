"""Tests for hermes_cli/concierge.py — the ``hermes concierge`` operator CLI.

Mirrors the cron CLI test pattern (subcommand parser builder + direct handler
calls, as in tests/hermes_cli/test_cron.py) and the concierge provision
tests' temp-home fixture (HERMES_HOME + Path.home patched — create_profile
resolves profiles/ under the real home otherwise, and tests must never touch
~/.hermes).

Task 4.4 acceptance contract locked here: ``hermes concierge new reminders
--channel 123`` inside a temp home provisions the bot and exits 0 with the
profile name on stdout, and ``hermes concierge list`` lists it. Plus the
exit-code mapping (unknown blueprint/profile -> 1), provider-group resolution
through the real config loader, retire semantics (pause + unbind, profile
kept unless --delete), the raw ``bindings`` view, and registration parity
with cron/webhook (builtin-subcommand set + handler wiring).

M4b review contracts locked here:
- ``retire`` is IDENTITY-guarded, not just existence-guarded: ``default``
  (trivially "existing") and any plain non-bot profile are refused with exit 1
  and NOTHING unbound or deleted — profile_exists() alone used to pass both;
- a ``bot-``-prefixed profile whose binding is already gone (orphaned
  mid-retire) still retires — the prefix fallback exists for exactly that;
- ``new`` on an already-provisioned channel prints "Already provisioned",
  never "Created", and the response-only ``already_provisioned`` flag never
  reaches the stored binding;
- a provision-lock TimeoutError surfaces as ``Error: ...`` + exit 1 (the
  CLI's catch of provision's fail-closed lock), leaving nothing built.
"""

from __future__ import annotations

import argparse
import json
import os
from argparse import Namespace
from pathlib import Path

import pytest

from concierge import provision, registry
from hermes_cli.concierge import concierge_command
from hermes_cli.main import _BUILTIN_SUBCOMMANDS, cmd_concierge
from hermes_cli.subcommands.concierge import build_concierge_parser


@pytest.fixture
def concierge_home(tmp_path, monkeypatch):
    # The NESTED shape (Path.home -> tmp_path, HERMES_HOME -> tmp_path/.hermes)
    # is load-bearing, not incidental:
    # - create_profile resolves profiles/ via get_default_hermes_root(), which
    #   special-cases HERMES_HOME: a value that is NOT under Path.home()/.hermes
    #   takes the Docker/custom-root branch (HERMES_HOME itself becomes the
    #   profiles root's parent), while tmp/.hermes stays on the NORMAL branch —
    #   the exact resolution production ~/.hermes uses. A flat HERMES_HOME would
    #   exercise a different code path than the one operators run.
    # - Path.home must ALSO be patched: some call sites anchor on Path.home()
    #   directly, outside any HERMES_HOME resolution (delete_profile's wrapper
    #   dir is ~/.local/bin). With only HERMES_HOME patched, those sites would
    #   read/write the REAL home — tests must never touch ~/.hermes or ~.
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def _parse(argv):
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_concierge_parser(subparsers, cmd_concierge=concierge_command)
    return parser.parse_args(argv)


def _run(argv):
    return concierge_command(_parse(argv))


def _visible_profiles(home):
    """Real profile dirs under profiles/ — the canonical delete leaves a
    hidden ``.``-prefixed tombstone behind, which is not a profile."""
    root = home / "profiles"
    if not root.is_dir():
        return []
    return [d for d in root.iterdir() if not d.name.startswith(".")]


class TestNew:

    def test_provisions_bot_exit_0_and_prints_profile(self, concierge_home, capsys):
        rc = _run(["concierge", "new", "reminders", "--channel", "123"])
        assert rc == 0
        out = capsys.readouterr().out
        binding = registry.get_binding("123")
        assert binding is not None, "channel 123 must be bound"
        assert binding["profile"] in out
        assert (concierge_home / "profiles" / binding["profile"]).is_dir()

    def test_guild_and_display_name_recorded(self, concierge_home):
        assert _run(["concierge", "new", "reminders", "--channel", "7",
                     "--guild", "42", "--name", "House Bot"]) == 0
        binding = registry.get_binding("7")
        assert binding["guild_id"] == "42"
        # the display name is the bot's own persona identity (SOUL {name}),
        # never the Discord picker copy
        soul = (concierge_home / "profiles" / binding["profile"] / "SOUL.md").read_text()
        assert "House Bot" in soul

    def test_resolves_provider_group_from_config(self, concierge_home):
        (concierge_home / "config.yaml").write_text(
            "concierge:\n"
            "  provider_groups:\n"
            "    eu:\n"
            "      provider: nous\n"
            "      model: z-ai/glm-5.3\n"
            "      guild_ids: ['42']\n"
        )
        assert _run(["concierge", "new", "reminders", "--channel", "9",
                     "--guild", "42"]) == 0
        binding = registry.get_binding("9")
        assert binding["provider_group"] == "eu"
        cfg_text = (concierge_home / "profiles" / binding["profile"] / "config.yaml").read_text()
        assert "z-ai/glm-5.3" in cfg_text  # group model beat the blueprint preset

    def test_misconfigured_provider_group_fails_loudly(self, concierge_home, capsys):
        (concierge_home / "config.yaml").write_text(
            "concierge:\n"
            "  default_provider_group: broken\n"
            "  provider_groups:\n"
            "    broken:\n"
            "      provider: nous\n"  # missing 'model'
        )
        rc = _run(["concierge", "new", "reminders", "--channel", "11"])
        assert rc == 1
        assert "missing required key" in capsys.readouterr().out
        # nothing half-built survived the refusal
        assert registry.get_binding("11") is None
        assert _visible_profiles(concierge_home) == []

    def test_unknown_blueprint_exit_1_with_known_keys(self, concierge_home, capsys):
        rc = _run(["concierge", "new", "nope", "--channel", "123"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "Unknown blueprint" in out
        assert "reminders" in out  # known keys listed for the operator

    def test_idempotent_per_channel(self, concierge_home):
        assert _run(["concierge", "new", "reminders", "--channel", "123"]) == 0
        assert _run(["concierge", "new", "reminders", "--channel", "123"]) == 0
        assert len(_visible_profiles(concierge_home)) == 1

    def test_already_provisioned_prints_truthful_header(self, concierge_home, capsys):
        """The idempotent second `new` must say "Already provisioned" —
        "Created" would be a lie — and the response-only flag must not leak
        into the STORED binding (it is CLI response metadata, not registry
        data; every other binding reader would see an unknown key)."""
        assert _run(["concierge", "new", "reminders", "--channel", "123"]) == 0
        capsys.readouterr()
        rc = _run(["concierge", "new", "reminders", "--channel", "123"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Already provisioned" in out
        assert "Created Concierge bot" not in out
        # the stored binding carries the registry's own keys only
        binding = registry.get_binding("123")
        assert binding is not None
        assert "already_provisioned" not in binding
        # and a first (real) creation on a FRESH channel says "Created", never
        # "Already" — the truthful header cuts both ways
        assert _run(["concierge", "new", "reminders", "--channel", "456"]) == 0
        fresh_out = capsys.readouterr().out
        assert "Created Concierge bot" in fresh_out
        assert "Already provisioned" not in fresh_out


class TestList:

    def test_lists_provisioned_bot(self, concierge_home, capsys):
        _run(["concierge", "new", "reminders", "--channel", "123"])
        rc = _run(["concierge", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        profile = registry.get_binding("123")["profile"]
        assert profile in out
        assert "123" in out
        assert "Reminder Bot" in out  # blueprint title, not just the key

    def test_empty_home_lists_blueprint_hint(self, concierge_home, capsys):
        rc = _run(["concierge", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "No concierge bots" in out
        assert "reminders" in out

    def test_bare_invocation_defaults_to_list(self, concierge_home, capsys):
        rc = _run(["concierge"])
        assert rc == 0
        assert "No concierge bots" in capsys.readouterr().out


class TestRetire:

    def _provision(self, home, channel="123"):
        assert _run(["concierge", "new", "reminders", "--channel", channel]) == 0
        return registry.get_binding(channel)["profile"]

    def test_unbinds_pauses_and_keeps_profile(self, concierge_home, capsys):
        profile = self._provision(concierge_home)
        rc = _run(["concierge", "retire", profile])
        assert rc == 0
        assert registry.get_binding("123") is None
        # routine paused in the bot's OWN cron store (cron's pause contract:
        # enabled=False + state="paused" — cron has no 'paused' field)
        jobs = json.loads(
            (concierge_home / "profiles" / profile / "cron" / "jobs.json").read_text()
        )["jobs"]
        assert len(jobs) == 1
        assert jobs[0]["enabled"] is False
        assert jobs[0]["state"] == "paused"
        # profile kept by default (reversible retire; --delete opts into removal)
        assert (concierge_home / "profiles" / profile).is_dir()
        out = capsys.readouterr().out
        assert profile in out and "123" in out

    def test_delete_flag_removes_profile(self, concierge_home):
        profile = self._provision(concierge_home)
        assert _run(["concierge", "retire", profile, "--delete"]) == 0
        assert registry.get_binding("123") is None
        assert not (concierge_home / "profiles" / profile).exists()

    def test_unknown_profile_exit_1(self, concierge_home, capsys):
        rc = _run(["concierge", "retire", "no-such-bot"])
        assert rc == 1
        assert "no-such-bot" in capsys.readouterr().out

    def test_retire_default_refused_nothing_touched(self, concierge_home, capsys):
        """``default`` is the operator's own hub home, and profile_exists()
        alone says it exists — the IDENTITY guard must refuse it BEFORE any
        retire step runs. A guarded refusal must also leave every binding
        alone (the registry is the commit pointer: nothing was retired)."""
        binding = registry.get_binding("123")
        assert binding is None  # nothing provisioned yet: the guard stands alone
        rc = _run(["concierge", "retire", "default"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "not a Concierge bot" in out
        # nothing was bound and nothing was provisioned before or after
        assert registry.list_bindings() == {}
        assert _visible_profiles(concierge_home) == []

    def test_retire_default_refused_even_when_channel_bound(self, concierge_home, capsys):
        """The dangerous case: a bot IS provisioned (registry non-empty), and
        the operator types `retire default`. The refusal must fire before the
        bound bot's state (or the hub home) is touched at all."""
        profile = self._provision(concierge_home)
        rc = _run(["concierge", "retire", "default"])
        assert rc == 1
        assert "not a Concierge bot" in capsys.readouterr().out
        # the REAL bot is untouched: still bound, routine still enabled, and
        # the hub home's own files (concurrency locks, registry) intact
        binding = registry.get_binding("123")
        assert binding is not None and binding["profile"] == profile
        jobs = json.loads(
            (concierge_home / "profiles" / profile / "cron" / "jobs.json")
            .read_text(encoding="utf-8")
        )["jobs"]
        assert jobs[0]["enabled"] is not False
        assert (concierge_home / "concierge" / "registry.json").is_file()

    def test_retire_plain_profile_refused(self, concierge_home, capsys):
        """A REAL profile that is NOT a bot (no binding, no bot- prefix) is
        refused even though profile_exists() is True — retire may never pause,
        unbind or delete a profile the concierge did not provision. Its cron
        store (a human profile's real jobs) must survive untouched."""
        from hermes_cli.profiles import create_profile

        human = create_profile("work", description="a human profile")
        (human / "cron").mkdir(parents=True, exist_ok=True)
        (human / "cron" / "jobs.json").write_text(
            json.dumps({"jobs": [{"id": "j1", "name": "standup", "enabled": True}]}),
            encoding="utf-8",
        )
        rc = _run(["concierge", "retire", "work"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "not a Concierge bot" in out
        # the human profile and its jobs are untouched
        assert human.is_dir()
        jobs = json.loads(
            (human / "cron" / "jobs.json").read_text(encoding="utf-8")
        )["jobs"]
        assert jobs[0]["enabled"] is True
        assert registry.list_bindings() == {}

    def test_retire_orphaned_bot_profile_still_retires(self, concierge_home, capsys):
        """The prefix fallback: a bot whose binding is ALREADY gone (the
        mid-retire crash state: pause done, unbind died) is still retirable —
        that is exactly what the ``bot-`` fallback exists for. Retiring it
        must finish the teardown (no double-failure on the missing binding)."""
        profile = self._provision(concierge_home)
        assert registry.unbind_channel("123") is True  # simulate the crash state
        rc = _run(["concierge", "retire", profile])
        assert rc == 0
        assert profile in capsys.readouterr().out
        # the profile survived (retire without --delete keeps it) but its
        # routine is paused — the teardown finished despite no binding
        jobs = json.loads(
            (concierge_home / "profiles" / profile / "cron" / "jobs.json")
            .read_text(encoding="utf-8")
        )["jobs"]
        assert jobs[0]["enabled"] is False and jobs[0]["state"] == "paused"


class TestBindings:

    def test_lists_channel_to_profile(self, concierge_home, capsys):
        _run(["concierge", "new", "reminders", "--channel", "123", "--guild", "42"])
        rc = _run(["concierge", "bindings"])
        assert rc == 0
        out = capsys.readouterr().out
        profile = registry.get_binding("123")["profile"]
        assert "123" in out and profile in out and "reminders" in out

    def test_empty(self, concierge_home, capsys):
        assert _run(["concierge", "bindings"]) == 0
        assert "No channel bindings" in capsys.readouterr().out


class TestProvisionLockTimeout:
    """The CLI must SURFACE provision's fail-closed lock as ``Error: ...`` +
    exit 1 (the ``except (KeyError, TimeoutError)`` branch in _cmd_new), not
    crash with a traceback or silently provision anyway. flock is per-fd, so a
    second fd on the lock FILE held open IN THIS PROCESS conflicts — no child
    process needed (verified: a LOCK_EX|LOCK_NB on a second fd raises EAGAIN
    while the first holds it)."""

    def test_timeout_surfaces_as_error_exit_1(self, concierge_home, monkeypatch, capsys):
        import fcntl

        lock_path = provision._provision_lock_path("123")
        lock_path.parent.mkdir(parents=True, exist_ok=True)  # fresh home has no concierge/ yet
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # hold channel 123's lock
            monkeypatch.setattr(provision, "_PROVISION_LOCK_TIMEOUT_SECONDS", 0.5)
            rc = _run(["concierge", "new", "reminders", "--channel", "123"])
            assert rc == 1
            out = capsys.readouterr().out
            assert out.startswith("Error:")
            assert "provision lock" in out
            # fail closed: no bot, no profile, no binding was built
            assert registry.get_binding("123") is None
            assert _visible_profiles(concierge_home) == []
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def test_timeout_error_names_channel_and_retries_cleanly(self, concierge_home, monkeypatch, capsys):
        """The message is operator-actionable (names the channel and the
        retry), and once the lock frees, the same command provisions
        normally — a TimeoutError is a retryable refusal, not corruption."""
        import fcntl

        lock_path = provision._provision_lock_path("123")
        lock_path.parent.mkdir(parents=True, exist_ok=True)  # fresh home has no concierge/ yet
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            monkeypatch.setattr(provision, "_PROVISION_LOCK_TIMEOUT_SECONDS", 0.5)
            assert _run(["concierge", "new", "reminders", "--channel", "123"]) == 1
            out = capsys.readouterr().out
            # operator-actionable: names the channel and says to retry
            assert out.startswith("Error:") and "123" in out and "retry" in out
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        # lock released: the SAME command succeeds and binds
        assert _run(["concierge", "new", "reminders", "--channel", "123"]) == 0
        out = capsys.readouterr().out
        assert "Created Concierge bot" in out
        assert registry.get_binding("123") is not None
        assert len(_visible_profiles(concierge_home)) == 1


class TestParserAndRegistration:

    @staticmethod
    def _sentinel(args):  # pragma: no cover - only identity is asserted
        return "concierge-handler"

    def _build(self):
        parser = argparse.ArgumentParser(prog="hermes")
        subparsers = parser.add_subparsers(dest="command")
        build_concierge_parser(subparsers, cmd_concierge=self._sentinel)
        return parser

    def test_actions_flags_and_dispatch(self):
        parser = self._build()
        ns = parser.parse_args(["concierge", "new", "reminders", "--channel", "9"])
        assert ns.command == "concierge"
        assert ns.concierge_action == "new"
        assert ns.blueprint == "reminders"
        assert ns.channel == "9"
        assert ns.guild is None and ns.name is None
        assert ns.func is self._sentinel
        # 'ls' alias maps to the list action
        assert parser.parse_args(["concierge", "ls"]).concierge_action == "ls"
        assert parser.parse_args(["concierge", "retire", "p", "--delete"]).delete is True
        # bare `hermes concierge` parses; the handler defaults it to list
        assert parser.parse_args(["concierge"]).concierge_action is None

    def test_channel_required_for_new(self):
        with pytest.raises(SystemExit):
            self._build().parse_args(["concierge", "new", "reminders"])

    def test_registered_like_cron_and_webhook(self):
        # Module-level import (top of file): hermes_cli.main's first import runs
        # hermes_bootstrap, which stats <repo>/../manifest.json — inside the guarded
        # test window an in-function import refuses as home I/O when this checkout
        # lives under ~/.hermes. Collection-time import matches every sibling CLI
        # test file and is guard-free by design.
        assert "concierge" in _BUILTIN_SUBCOMMANDS
        assert callable(cmd_concierge)

    def test_unknown_action_prints_usage_exit_1(self, capsys):
        assert concierge_command(Namespace(concierge_action="bogus")) == 1
        assert "Usage" in capsys.readouterr().out
