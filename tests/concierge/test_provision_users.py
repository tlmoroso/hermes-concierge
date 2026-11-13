"""User-keyed provisioning contracts (orchestrator redesign, T3): user-owned
channel-less bots, per-user idempotence and locking, users-section
bookkeeping with transactional rollback, reprovision parity, the per-user
cron attach rule, and retire/status cleanup of users ownership.

Mirrors the test_provision.py rig (temp HERMES_HOME + patched Path.home —
tests must never touch ~/.hermes).
"""

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from concierge import provision, registry
from hermes_yaml import safe_load as _yaml_safe_load

# Repo root (parent of the ``concierge`` package) so child processes can
# import it (the cross-process lock test, mirroring test_provision.py).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(provision.__file__)))


def _yaml(path):
    return _yaml_safe_load(Path(path).read_text(encoding="utf-8"))


@pytest.fixture
def concierge_home(tmp_path, monkeypatch):
    # Same NESTED shape as test_provision.py (load-bearing, see there):
    # HERMES_HOME under Path.home keeps the NORMAL profile-root resolution,
    # and Path.home itself is patched so direct Path.home() call sites never
    # touch the real home.
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def _visible_profiles(home):
    """Real profile dirs under profiles/ (a canonical delete leaves a hidden
    ``.deleted`` tombstone behind, which is not a profile)."""
    d = home / "profiles"
    if not d.exists():
        return []
    return [p for p in d.iterdir() if not p.name.startswith(".")]


def _bot_store_jobs(home, profile):
    jobs_file = home / "profiles" / profile / "cron" / "jobs.json"
    if not jobs_file.exists():
        return []
    import json

    return json.loads(jobs_file.read_text())["jobs"]


def _provisioned(home, key="orchestrator", user_id="u-1", **kwargs):
    """The user-owned provision helper (channel-less unless channel_id is in
    kwargs)."""
    kwargs.setdefault("source_skills_dir", home / "skills")
    return provision.create_bot(key, user_id=user_id, **kwargs)


def _bound(home, key="reminders", channel_id="123", **kwargs):
    kwargs.setdefault("source_skills_dir", home / "skills")
    return provision.create_bot(key, channel_id=channel_id, **kwargs)


# ---------------------------------------------------------------------------
# Mode selection + user-keyed idempotence
# ---------------------------------------------------------------------------

def test_create_bot_without_channel_or_user_raises(concierge_home):
    """Neither flag: an operator-actionable ValueError naming BOTH modes —
    not a TypeError from a missing keyword argument."""
    with pytest.raises(ValueError, match="channel_id") as excinfo:
        provision.create_bot("reminders", source_skills_dir=concierge_home / "skills")
    assert "user_id" in str(excinfo.value)
    assert _visible_profiles(concierge_home) == []


def test_user_owned_orchestrator_is_idempotent_per_user(concierge_home):
    """A channel-less orchestrator provision writes NO binding; ownership is
    the users entry. A second call for the same user returns the SAME entry
    annotated ``already_provisioned: True`` — one profile, ever."""
    first = _provisioned(concierge_home)
    assert first["orchestrator"].startswith("bot-orchestrator-")
    assert "already_provisioned" not in first
    entry = registry.get_user("u-1")
    assert entry["orchestrator"] == first["orchestrator"]
    assert entry["home_channel"] is None  # bootstrap sets it later, not provision
    assert registry.orchestrator_for_user("u-1") == first["orchestrator"]
    # channel-less: NO binding was written
    assert registry.list_bindings() == {}
    assert registry.channels_for_profile(first["orchestrator"]) == []

    again = _provisioned(concierge_home)
    assert again["already_provisioned"] is True
    assert again["orchestrator"] == first["orchestrator"]
    assert len(_visible_profiles(concierge_home)) == 1


def test_channel_less_worker_provision_writes_no_binding(concierge_home):
    """A per-user worker: profile created, NO binding, workers-map entry
    written. ``user_id`` still names the per-user terminal.cwd sandbox."""
    registry.set_user("u-1", orchestrator="bot-orchestrator-seed")
    result = _provisioned(concierge_home, key="reminders")
    assert result["profile"].startswith("bot-reminders-")
    assert result["blueprint"] == "reminders"
    assert "already_provisioned" not in result
    assert (concierge_home / "profiles" / result["profile"]).is_dir()
    assert registry.list_bindings() == {}
    assert registry.worker_for_user("u-1", "reminders") == result["profile"]
    # the users entry survives with its orchestrator intact
    assert registry.orchestrator_for_user("u-1") == "bot-orchestrator-seed"
    # per-user sandbox seed (provision.py's original user_id meaning)
    cfg = _yaml(concierge_home / "profiles" / result["profile"] / "config.yaml")
    assert cfg["terminal"]["cwd"] == str(concierge_home / "workspaces" / "u-1")
    # idempotent second call: same profile, ONE profile dir, flagged
    again = _provisioned(concierge_home, key="reminders")
    assert again == {
        "profile": result["profile"],
        "blueprint": "reminders",
        "already_provisioned": True,
    }
    assert len(_visible_profiles(concierge_home)) == 1
    # status reports the ownership (user_id + channel_less for /bot list)
    status = provision.bot_status(result["profile"])
    assert status["user_id"] == "u-1"
    assert status["channel_less"] is True
    assert status["channels"] == []


def test_channel_less_worker_provision_requires_a_user_entry(concierge_home):
    """registry.set_user_worker is the worker bookkeeping and the user entry
    is its prerequisite (set_user is the only creation path — registry
    contract). A stray worker write for an unknown user must FAIL LOUDLY and
    roll back, never silently drop the ownership record idempotence keys
    off."""
    with pytest.raises(ValueError, match="orchestrator first"):
        _provisioned(concierge_home, key="reminders")
    assert registry.get_user("u-1") is None
    assert _visible_profiles(concierge_home) == []


# ---------------------------------------------------------------------------
# Per-user provision lock
# ---------------------------------------------------------------------------

@pytest.mark.skipif(registry.fcntl is None, reason="POSIX fcntl/flock required")
def test_concurrent_user_provisions_yield_one_bot(concierge_home):
    """Two THREADS provisioning the same USER concurrently: exactly one
    profile and one users entry — the per-user lock serializes the
    read-then-act idempotence check (the channel lock's TOCTOU rationale,
    keyed by user)."""
    results, errors = [], []

    def _worker():
        try:
            results.append(_provisioned(concierge_home))
        except Exception as e:  # pragma: no cover - surfaced by the assert
            errors.append(e)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, f"concurrent provisions raised: {errors}"
    assert len(results) == 4
    assert len({r["orchestrator"] for r in results}) == 1
    assert len(_visible_profiles(concierge_home)) == 1
    assert len(registry.list_users()) == 1


@pytest.mark.skipif(registry.fcntl is None, reason="POSIX fcntl/flock required")
def test_user_provision_lock_is_per_user_and_fails_closed(
        concierge_home, monkeypatch):
    """The user lock is a SEPARATE lock (key ``user:<id>``): held for one user
    it fails a concurrent provision of that user CLOSED (TimeoutError, nothing
    provisioned) while a DIFFERENT user's provision sails through."""
    ready = concierge_home / "child_holds_u1"
    release = concierge_home / "child_may_release"
    holder = concierge_home / "holder.py"
    holder.write_text(
        textwrap.dedent(
            f"""
            import os, sys, time, pathlib
            sys.path.insert(0, {_REPO_ROOT!r})
            os.environ["HERMES_HOME"] = {str(concierge_home)!r}
            from concierge import provision

            with provision._channel_provision_lock("user:u-1"):
                pathlib.Path({str(ready)!r}).write_text("1")
                for _ in range(600):
                    if pathlib.Path({str(release)!r}).exists():
                        break
                    time.sleep(0.05)
            """
        )
    )
    child = subprocess.Popen([sys.executable, str(holder)])
    try:
        for _ in range(1000):
            if ready.exists():
                break
            time.sleep(0.01)
        assert ready.exists(), "child never acquired the user provision lock"
        monkeypatch.setattr(provision, "_PROVISION_LOCK_TIMEOUT_SECONDS", 0.5)
        with pytest.raises(TimeoutError, match="provision lock"):
            _provisioned(concierge_home)  # u-1, whose lock the child holds
        assert registry.get_user("u-1") is None
        assert _visible_profiles(concierge_home) == []
        # PER-user, not global: u-2's provision is not blocked by u-1's lock
        other = _provisioned(concierge_home, user_id="u-2")
        assert registry.orchestrator_for_user("u-2") == other["orchestrator"]
    finally:
        release.write_text("1")
        child.wait(timeout=30)


# ---------------------------------------------------------------------------
# Transactional rollback covers the users entry
# ---------------------------------------------------------------------------

def test_failed_provision_rolls_back_users_entry(concierge_home, monkeypatch):
    """both-given orchestrator whose BIND (the last step) fails: the users
    entry written by the bookkeeping is rolled back with the profile — no
    ownership record pointing at a bot that does not exist."""
    def _boom(*args, **kwargs):
        raise RuntimeError("registry write failed")

    monkeypatch.setattr(registry, "bind_channel", _boom)
    with pytest.raises(RuntimeError, match="registry write failed"):
        provision.create_bot(
            "orchestrator", channel_id="123", user_id="u-1",
            source_skills_dir=concierge_home / "skills")
    assert registry.get_user("u-1") is None
    assert registry.get_binding("123") is None
    assert _visible_profiles(concierge_home) == []


def test_failed_worker_provision_rolls_back_worker_record(
        concierge_home, monkeypatch):
    """A channel-less worker whose ROUTINE step fails: the workers-map entry
    is rolled back with the profile; the user's pre-existing entry (and its
    orchestrator) survives untouched."""
    from cron import jobs as cron_jobs

    # a home channel is required for the routine step to run (no delivery
    # channel -> the routine is DEFERRED, and the failure never fires)
    registry.set_user("u-1", orchestrator="bot-orchestrator-seed",
                      home_channel="ch-home")

    def _boom(*args, **kwargs):
        raise RuntimeError("cron store exploded")

    monkeypatch.setattr(cron_jobs, "create_job", _boom)
    with pytest.raises(RuntimeError, match="cron store exploded"):
        _provisioned(concierge_home, key="reminders")
    assert registry.worker_for_user("u-1", "reminders") is None
    assert registry.orchestrator_for_user("u-1") == "bot-orchestrator-seed"
    assert _visible_profiles(concierge_home) == []


# ---------------------------------------------------------------------------
# retire cleans users-section ownership
# ---------------------------------------------------------------------------

def test_retire_cleans_users_entry_orchestrator_shape(concierge_home):
    first = _provisioned(concierge_home)
    provision.retire_bot(first["orchestrator"])
    assert registry.get_user("u-1") is None
    assert registry.orchestrator_for_user("u-1") is None


def test_retire_cleans_users_entry_worker_shape(concierge_home):
    """Retiring a worker removes ITS workers-map record; the user entry (and
    its orchestrator, home channel) survives."""
    registry.set_user("u-1", orchestrator="bot-orchestrator-seed",
                      home_channel="ch-home")
    worker = _provisioned(concierge_home, key="reminders")
    provision.retire_bot(worker["profile"])
    assert registry.worker_for_user("u-1", "reminders") is None
    entry = registry.get_user("u-1")
    assert entry["orchestrator"] == "bot-orchestrator-seed"
    assert entry["home_channel"] == "ch-home"


def test_retire_of_unowned_bot_leaves_users_untouched(concierge_home):
    """A v1 channel bot owns no users entries: retiring it is a no-op for the
    users section (and tolerant when the section is empty)."""
    registry.set_user("u-9", orchestrator="bot-orchestrator-other")
    binding = _bound(concierge_home)
    provision.retire_bot(binding["profile"])
    assert registry.get_user("u-9") is not None
    assert registry.get_binding("123") is None


# ---------------------------------------------------------------------------
# Cron attach rule (design §6): per-user bots never skip
# ---------------------------------------------------------------------------

def test_user_owned_routine_attaches_even_in_multi_user(
        concierge_home, monkeypatch):
    """A user-owned bot's routine cannot leak another user's data, so
    multi-user mode does NOT skip it. The routine's origin is the bot's
    DELIVERY channel: the user's home channel when no channel is bound."""
    monkeypatch.setattr(provision, "_concierge_multi_user", lambda: True)
    registry.set_user("u-1", orchestrator="bot-orchestrator-seed",
                      home_channel="ch-home")
    result = _provisioned(concierge_home, key="reminders")
    jobs = _bot_store_jobs(concierge_home, result["profile"])
    assert jobs, "user-owned bot must keep its routine in multi-user mode"
    job = next(
        j for j in jobs
        if j["name"].startswith(f"[bot:{result['profile']}]")
    )
    assert job["origin"] == {"platform": "discord", "chat_id": "ch-home"}


def test_user_owned_routine_deferred_without_delivery_channel(
        concierge_home, monkeypatch, caplog):
    """No delivery channel known (channel-less bot, user has no home channel
    yet — the pre-bootstrap orchestrator shape): the routine is DEFERRED —
    skipped with one debug log, no cron job with a null origin, and the
    provision itself still succeeds."""
    import logging

    monkeypatch.setattr(provision, "_concierge_multi_user", lambda: True)
    registry.set_user("u-1", orchestrator="bot-orchestrator-seed")  # no home channel
    with caplog.at_level(logging.DEBUG, logger="concierge.provision"):
        result = _provisioned(concierge_home, key="reminders")
    assert _bot_store_jobs(concierge_home, result["profile"]) == []
    assert "deferred" in caplog.text
    assert registry.worker_for_user("u-1", "reminders") == result["profile"]


def test_v1_channel_bot_still_skips_routine_in_multi_user(
        concierge_home, monkeypatch):
    """The other side of the rule: a shared-channel bot (user_id None) keeps
    the multi-user skip — exactly v1."""
    monkeypatch.setattr(provision, "_concierge_multi_user", lambda: True)
    binding = _bound(concierge_home)
    assert _bot_store_jobs(concierge_home, binding["profile"]) == []


# ---------------------------------------------------------------------------
# both given: user-owned AND channel-bound
# ---------------------------------------------------------------------------

def test_both_given_provisions_user_owned_and_channel_bound(concierge_home):
    binding = provision.create_bot(
        "orchestrator", channel_id="123", user_id="u-1", guild_id="42",
        source_skills_dir=concierge_home / "skills")
    profile = binding["profile"]
    assert profile.startswith("bot-orchestrator-")
    # channel-bound: the v1 binding exists and routes to the bot
    assert registry.get_binding("123")["profile"] == profile
    assert registry.channels_for_profile(profile) == ["123"]
    # user-owned: the users entry records the orchestrator + guild
    entry = registry.get_user("u-1")
    assert entry["orchestrator"] == profile
    assert entry["guild_id"] == "42"
    assert entry["home_channel"] is None  # bootstrap sets it later, not provision


def test_both_given_adopts_the_channels_existing_bot(concierge_home):
    """The channel already has this blueprint's bot (v1): the both-given call
    is the v1 idempotent return, and the user's ownership lands on the
    EXISTING profile — user-owned AND channel-bound, no second bot."""
    registry.set_user("u-1", orchestrator="bot-orchestrator-seed")
    first = _bound(concierge_home)
    again = provision.create_bot(
        "reminders", channel_id="123", user_id="u-1",
        source_skills_dir=concierge_home / "skills")
    assert again["already_provisioned"] is True
    assert again["profile"] == first["profile"]
    assert registry.worker_for_user("u-1", "reminders") == first["profile"]
    assert len(_visible_profiles(concierge_home)) == 1


def test_orchestrator_reprovision_parity_retires_replaced_bot(concierge_home):
    """Reprovision parity: the user's replaced orchestrator is retired AND
    DELETED exactly like the channel path retires a replaced bot — no parked
    orphan per reprovision."""
    first = _provisioned(concierge_home)  # channel-less orchestrator #1
    second = provision.create_bot(
        "orchestrator", channel_id="123", user_id="u-1",
        source_skills_dir=concierge_home / "skills")
    assert second["profile"] != first["orchestrator"]
    assert not (concierge_home / "profiles" / first["orchestrator"]).exists()
    # the user owns exactly the new one, channel-bound
    assert registry.orchestrator_for_user("u-1") == second["profile"]
    assert registry.get_binding("123")["profile"] == second["profile"]


def test_worker_reprovision_parity_retires_replaced_worker(concierge_home):
    """Worker parity: the old worker profile is retired+deleted and the
    workers-map entry is OVERWRITTEN (never duplicated); the new bot's
    routine delivers to its bound channel."""
    registry.set_user("u-1", orchestrator="bot-orchestrator-seed")
    first = _provisioned(concierge_home, key="reminders")  # channel-less #1
    second = provision.create_bot(
        "reminders", channel_id="123", user_id="u-1",
        source_skills_dir=concierge_home / "skills")
    assert second["profile"] != first["profile"]
    assert not (concierge_home / "profiles" / first["profile"]).exists()
    assert registry.worker_for_user("u-1", "reminders") == second["profile"]
    assert registry.get_binding("123")["profile"] == second["profile"]
    job = next(
        j for j in _bot_store_jobs(concierge_home, second["profile"])
        if j["name"].startswith(f"[bot:{second['profile']}]")
    )
    assert job["origin"] == {"platform": "discord", "chat_id": "123"}


# ---------------------------------------------------------------------------
# bot_status user fields
# ---------------------------------------------------------------------------

def test_bot_status_reports_user_ownership(concierge_home):
    first = _provisioned(concierge_home)
    profile = first["orchestrator"]
    status = provision.bot_status(profile)
    assert status["user_id"] == "u-1"
    assert status["channel_less"] is True
    assert status["channels"] == []
    # once a home channel is bound (the bootstrap flow), the same profile
    # reports bound
    registry.bind_channel("123", profile=profile, blueprint="orchestrator")
    status = provision.bot_status(profile)
    assert status["channel_less"] is False
    assert status["channels"] == ["123"]
    assert status["user_id"] == "u-1"


def test_bot_status_of_unowned_bot_has_no_user_fields(concierge_home):
    """A v1 channel bot nobody owns keeps the exact v1 status shape."""
    binding = _bound(concierge_home)
    status = provision.bot_status(binding["profile"])
    assert "user_id" not in status
    assert "channel_less" not in status


# ---------------------------------------------------------------------------
# T3 quality fix: an orchestrator swap must not wipe the users entry's
# scalars/workers (the bootstrap-recorded home channel and every worker
# record would be destroyed while the worker PROFILES stay alive — the exact
# double-build the user-keyed idempotence exists to prevent)
# ---------------------------------------------------------------------------

def _populated_user(home, home_channel="ch-home", guild_id="42"):
    """A user with a provisioned channel-less orchestrator, a
    bootstrap-shaped home channel/guild on the entry, and a live worker —
    the populated-entry shape a swap must preserve."""
    first = _provisioned(home)
    registry.update_user("u-1", home_channel=home_channel, guild_id=guild_id)
    worker = _provisioned(home, key="reminders")
    return first, worker


def test_orchestrator_parity_swap_preserves_populated_entry(concierge_home):
    """Reprovision parity with a POPULATED entry: the old orchestrator is
    retired+deleted, and the fresh users write PRESERVES
    home_channel/guild_id/workers — ONLY the orchestrator field changes
    (the home channel is still the user's home channel; its binding is being
    rebound to the new profile by this same provision)."""
    first, worker = _populated_user(concierge_home)

    second = provision.create_bot(
        "orchestrator", channel_id="123", user_id="u-1",
        source_skills_dir=concierge_home / "skills")
    assert second["profile"] != first["orchestrator"]
    assert not (concierge_home / "profiles" / first["orchestrator"]).exists()
    entry = registry.get_user("u-1")
    assert entry["orchestrator"] == second["profile"]
    assert entry["home_channel"] == "ch-home"
    assert entry["guild_id"] == "42"
    assert entry["workers"] == {"reminders": worker["profile"]}
    # the worker's PROFILE survived the orchestrator swap — its record is
    # still truthful, so the next ensure-worker call will not double-build
    assert (concierge_home / "profiles" / worker["profile"]).is_dir()
    assert registry.get_binding("123")["profile"] == second["profile"]


def test_both_given_adopt_swap_preserves_populated_entry(concierge_home):
    """The both-given ADOPT path with a swap (the channel's orchestrator
    differs from the user's): same preserved-scalars contract — the
    channel's bot becomes the user's orchestrator with
    home_channel/guild_id/workers intact, the replaced orchestrator is
    retired+deleted, and the channel keeps its binding."""
    first, worker = _populated_user(concierge_home)
    # a DIFFERENT orchestrator already bound to the channel (another install)
    channel_bot = provision.create_bot(
        "orchestrator", channel_id="123",
        source_skills_dir=concierge_home / "skills")
    assert channel_bot["profile"] != first["orchestrator"]

    adopted = provision.create_bot(
        "orchestrator", channel_id="123", user_id="u-1",
        source_skills_dir=concierge_home / "skills")
    assert adopted["already_provisioned"] is True
    assert adopted["profile"] == channel_bot["profile"]
    entry = registry.get_user("u-1")
    assert entry["orchestrator"] == channel_bot["profile"]
    assert entry["home_channel"] == "ch-home"
    assert entry["guild_id"] == "42"
    assert entry["workers"] == {"reminders": worker["profile"]}
    assert not (concierge_home / "profiles" / first["orchestrator"]).exists()
    assert (concierge_home / "profiles" / worker["profile"]).is_dir()
    assert registry.get_binding("123")["profile"] == channel_bot["profile"]


def test_retire_of_orchestrator_preserves_worker_records(concierge_home):
    """Plain retire_bot of an orchestrator: the entry's workers records (and
    guild_id) survive — the worker PROFILES stay alive, so their records
    must too — while the orchestrator and its home channel are cleared; the
    worker's next provision call stays idempotent."""
    first, worker = _populated_user(concierge_home)
    provision.retire_bot(first["orchestrator"])
    entry = registry.get_user("u-1")
    assert entry is not None
    assert entry.get("orchestrator") is None
    assert entry["home_channel"] is None
    assert entry["guild_id"] == "42"
    assert entry["workers"] == {"reminders": worker["profile"]}
    assert (concierge_home / "profiles" / worker["profile"]).is_dir()
    # the preserved record keeps the worker provision idempotent (no rebuild)
    again = _provisioned(concierge_home, key="reminders")
    assert again == {"profile": worker["profile"], "blueprint": "reminders",
                     "already_provisioned": True}
    # plain retire (no delete_profile): the orchestrator's own dir stays
    # (paused) — the contract under test is the users ENTRY, not the disk
    assert len(_visible_profiles(concierge_home)) == 2


def test_retire_of_bare_orchestrator_leaves_no_empty_shell(concierge_home):
    """An orchestrator whose entry has NO workers: retiring it removes the
    entry entirely — no orchestrator-less, worker-less shell in the file."""
    import json

    first = _provisioned(concierge_home)
    provision.retire_bot(first["orchestrator"])
    assert registry.get_user("u-1") is None
    raw = json.loads(
        (concierge_home / "concierge" / "registry.json").read_text())
    assert "u-1" not in raw["users"]


# ---------------------------------------------------------------------------
# T3 quality minors
# ---------------------------------------------------------------------------

def test_both_given_worker_adopt_requires_a_user_entry(concierge_home):
    """Missing adopt-path contract: both-given WORKER provision for a user
    with NO user entry raises the bookkeeping helper's ValueError — the
    channel keeps its bot (binding untouched, the existing profile neither
    rebuilt nor deleted) and nothing half-built is left behind."""
    binding = _bound(concierge_home)  # the v1 reminders bot on channel 123
    with pytest.raises(ValueError, match="orchestrator first"):
        provision.create_bot(
            "reminders", channel_id="123", user_id="u-1",
            source_skills_dir=concierge_home / "skills")
    assert registry.get_user("u-1") is None
    assert registry.get_binding("123")["profile"] == binding["profile"]
    assert len(_visible_profiles(concierge_home)) == 1


def test_channel_less_worker_persists_guild_id(concierge_home):
    """A channel-less WORKER has no binding to carry the guild_id — persist
    it to the user entry instead (registry.update_user). Idempotent: a later
    provision without one never overwrites the stored value."""
    registry.set_user("u-1", orchestrator="bot-orchestrator-seed")
    worker = _provisioned(concierge_home, key="reminders", guild_id="42")
    assert worker["blueprint"] == "reminders"
    assert registry.get_user("u-1")["guild_id"] == "42"
    # a second worker blueprint provisioned WITHOUT a guild_id keeps it
    other = _provisioned(concierge_home, key="researcher")
    assert other["blueprint"] == "researcher"
    assert registry.get_user("u-1")["guild_id"] == "42"
    assert registry.worker_for_user("u-1", "researcher") == other["profile"]


@pytest.mark.skipif(registry.fcntl is None, reason="POSIX fcntl/flock required")
def test_both_given_and_channel_less_provisions_do_not_deadlock(
        concierge_home, monkeypatch):
    """Regression for the fixed channel-then-user ExitStack ordering: one
    thread provisions BOTH-GIVEN (channel 123 + user u-1 — takes the channel
    lock, then the user lock), one provisions CHANNEL-LESS (user u-1 — takes
    only the user lock), started together. A lock-order cycle would hang or
    raise TimeoutError; both must complete with exactly one orchestrator."""
    barrier = threading.Barrier(2, timeout=30)
    results, errors = [], []
    real_create_profile = provision.create_profile

    def _slow_create_profile(*args, **kwargs):
        # a small real delay: widen the locked window so the two provisions
        # genuinely contend for the per-user lock (no generous sleeps)
        time.sleep(0.05)
        return real_create_profile(*args, **kwargs)

    monkeypatch.setattr(provision, "create_profile", _slow_create_profile)

    def _both_given():
        barrier.wait()
        try:
            results.append(provision.create_bot(
                "orchestrator", channel_id="123", user_id="u-1",
                source_skills_dir=concierge_home / "skills"))
        except Exception as e:  # pragma: no cover - surfaced by the assert
            errors.append(e)

    def _channel_less():
        barrier.wait()
        try:
            results.append(_provisioned(concierge_home))
        except Exception as e:  # pragma: no cover - surfaced by the assert
            errors.append(e)

    threads = [threading.Thread(target=_both_given),
               threading.Thread(target=_channel_less)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), (
        "a both-given and a channel-less provision of one user deadlocked "
        "(channel-then-user lock-order regression)")
    assert not errors, f"provisions raised: {errors}"
    assert len(results) == 2
    # whichever order they serialized in, exactly ONE orchestrator exists,
    # the user owns it, and the channel is bound to it
    assert len(_visible_profiles(concierge_home)) == 1
    profile = registry.orchestrator_for_user("u-1")
    assert profile is not None
    assert registry.get_binding("123")["profile"] == profile
    assert len(registry.list_users()) == 1
