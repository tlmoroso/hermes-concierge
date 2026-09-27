"""Registry contracts: bind/unbind round-trip, atomic persistence, profile-safety,
corruption tolerance, cross-process locking."""
import json
import logging
import os
import subprocess
import sys
import textwrap
import time

import pytest

from concierge import registry

# Repo root (parent of the ``concierge`` package) so child processes can import it.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(registry.__file__)))


def test_bind_and_get_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.bind_channel("1234567890", profile="bot-reminders-a1b2",
                          blueprint="reminders", guild_id="42")
    binding = registry.get_binding("1234567890")
    assert binding["profile"] == "bot-reminders-a1b2"
    assert binding["blueprint"] == "reminders"
    # persisted as JSON, one key per channel
    raw = json.loads((tmp_path / "concierge" / "registry.json").read_text())
    assert "1234567890" in raw["bindings"]


def test_unknown_channel_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert registry.get_binding("nope") is None


def test_unbind_removes_only_that_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.bind_channel("1", profile="a", blueprint="reminders")
    registry.bind_channel("2", profile="b", blueprint="researcher")
    registry.unbind_channel("1")
    assert registry.get_binding("1") is None
    assert registry.get_binding("2")["profile"] == "b"


def test_bindings_for_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.bind_channel("1", profile="a", blueprint="reminders")
    registry.bind_channel("2", profile="a", blueprint="reminders")
    registry.bind_channel("3", profile="b", blueprint="researcher")
    assert sorted(registry.channels_for_profile("a")) == ["1", "2"]


def test_corrupt_registry_starts_empty_not_crashing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "concierge").mkdir()
    (tmp_path / "concierge" / "registry.json").write_text("{not json")
    assert registry.get_binding("1") is None


def test_non_utf8_registry_degrades_to_empty_and_is_preserved(tmp_path, monkeypatch, caplog):
    """Binary garbage must not crash readers (UnicodeDecodeError is a ValueError,
    invisible to a bare JSONDecodeError/OSError guard) — and must be preserved
    before any save overwrites it, with a logged reason."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cdir = tmp_path / "concierge"
    cdir.mkdir()
    payload = b"\xff\xfe\x00 definitely not utf-8"
    (cdir / "registry.json").write_bytes(payload)
    with caplog.at_level(logging.WARNING, logger="concierge.registry"):
        assert registry.get_binding("1") is None
    preserved = cdir / "registry.json.corrupt"
    assert preserved.read_bytes() == payload
    assert "Corrupt file preserved at" in caplog.text


def test_non_dict_binding_values_are_skipped_not_fatal(tmp_path, monkeypatch):
    """One junk binding value must not crash every reader or cost the whole
    registry; the next save drops it (self-healing)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cdir = tmp_path / "concierge"
    cdir.mkdir()
    (cdir / "registry.json").write_text(json.dumps({
        "version": 1,
        "bindings": {
            "bad-str": "junk",
            "bad-list": ["junk"],
            "good": {"profile": "bot-a", "blueprint": "reminders"},
        }}))
    assert registry.get_binding("bad-str") is None
    assert registry.get_binding("bad-list") is None
    good = registry.get_binding("good")
    assert good is not None and good["profile"] == "bot-a"
    assert registry.channels_for_profile("bot-a") == ["good"]
    assert sorted(registry.list_bindings()) == ["good"]
    # a subsequent mutation persists only the valid entries
    registry.bind_channel("2", profile="bot-b", blueprint="notes")
    raw = json.loads((cdir / "registry.json").read_text())
    assert sorted(raw["bindings"]) == ["2", "good"]
    assert raw["version"] == 1


@pytest.mark.skipif(os.name == "nt", reason="chmod 0 is a POSIX guard")
def test_unreadable_registry_raises_but_warns_once(tmp_path, monkeypatch, caplog):
    """M5b re-review carry-forward: an unreadable (EACCES) registry file still
    RAISES OSError (loud-fail — the contents are fine, an empty degrade would
    be one save from erasing every binding), but the module's own WARNING —
    full traceback included — must fire ONCE per file path, not once per
    inbound event: the Discord hot path calls list_bindings() per message."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cdir = tmp_path / "concierge"
    cdir.mkdir()
    reg_file = cdir / "registry.json"
    reg_file.write_text(json.dumps({"version": 1, "bindings": {}}))
    reg_file.chmod(0)

    registry._unreadable_warned.clear()
    try:
        with caplog.at_level(logging.WARNING, logger="concierge.registry"):
            for _ in range(5):
                with pytest.raises(OSError):
                    registry.list_bindings()
        assert caplog.text.count("could not read") == 1
    finally:
        registry._unreadable_warned.clear()
        reg_file.chmod(0o644)

    # And a different unreadable path is a fresh incident (keyed by path).
    second_home = tmp_path / "second-home"
    monkeypatch.setenv("HERMES_HOME", str(second_home))
    reg2 = second_home / "concierge" / "registry.json"
    reg2.parent.mkdir(parents=True)
    reg2.write_text("{}")
    reg2.chmod(0)
    try:
        registry._unreadable_warned.clear()
        with caplog.at_level(logging.WARNING, logger="concierge.registry"):
            with pytest.raises(OSError):
                registry.get_binding("1")
        assert caplog.text.count("could not read") >= 1
    finally:
        registry._unreadable_warned.clear()
        reg2.chmod(0o644)


@pytest.mark.skipif(registry.fcntl is None, reason="POSIX fcntl/flock required")
def test_registry_lock_excludes_another_process(tmp_path, monkeypatch):
    """The registry lock must exclude a separate PROCESS (gateway vs. `hermes
    concierge` CLI), which a bare threading.Lock can never do — mirrors the cron
    jobs.json cross-process lock test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    ready = tmp_path / "child_holds_lock"
    release = tmp_path / "child_may_release"
    holder = tmp_path / "holder.py"
    holder.write_text(
        textwrap.dedent(
            f"""
            import os, sys, time, pathlib
            sys.path.insert(0, {_REPO_ROOT!r})
            os.environ["HERMES_HOME"] = {str(tmp_path)!r}
            from concierge import registry

            with registry._registry_lock():
                pathlib.Path({str(ready)!r}).write_text("1")
                # Hold the lock until the parent signals (bounded so a wedged
                # test can never hang CI).
                for _ in range(1000):
                    if pathlib.Path({str(release)!r}).exists():
                        break
                    time.sleep(0.01)
            """
        )
    )

    child = subprocess.Popen([sys.executable, str(holder)])
    try:
        # Wait until the child is inside the critical section.
        for _ in range(1000):
            if ready.exists():
                break
            time.sleep(0.01)
        assert ready.exists(), "child never acquired _registry_lock()"

        # While the child holds it, a non-blocking acquire of the SAME lock file
        # from this process must fail. A threading.Lock could never block here.
        lock_file = registry._registry_lock_file()
        assert lock_file == tmp_path / "concierge" / ".registry.lock"
        fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
        try:
            with pytest.raises(OSError):
                registry.fcntl.flock(fd, registry.fcntl.LOCK_EX | registry.fcntl.LOCK_NB)
        finally:
            os.close(fd)
    finally:
        release.write_text("1")
        child.wait(timeout=15)

    # Once the child has released, the lock is freely acquirable again and
    # mutations go through.
    with registry._registry_lock():
        pass
    registry.bind_channel("1", profile="a", blueprint="reminders")
    bound = registry.get_binding("1")
    assert bound is not None and bound["profile"] == "a"


# ── Task 7.1: rooms — ONE channel, SEVERAL bot members ────────────────────


def test_create_room_round_trip_and_persisted_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    room = registry.create_room("ch-1", ["bot-reminders-a1b2", "bot-scheduler-c3d4"])
    assert room["members"] == ["bot-reminders-a1b2", "bot-scheduler-c3d4"]
    assert room["created_at"]
    assert registry.room_members("ch-1") == ["bot-reminders-a1b2", "bot-scheduler-c3d4"]
    # rooms ride the SAME file as bindings (new top-level key)
    raw = json.loads((tmp_path / "concierge" / "registry.json").read_text())
    assert raw["rooms"]["ch-1"]["members"] == ["bot-reminders-a1b2", "bot-scheduler-c3d4"]
    assert raw["rooms"]["ch-1"]["created_at"]
    assert "bindings" in raw


def test_create_room_accepts_any_iterable_of_members(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.create_room("ch-1", ("bot-a", "bot-b"))  # tuple
    registry.create_room("ch-2", (m for m in ["bot-a"]))  # one-shot generator
    assert registry.room_members("ch-1") == ["bot-a", "bot-b"]
    assert registry.room_members("ch-2") == ["bot-a"]


def test_add_and_remove_members_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.create_room("ch-1", ["bot-a", "bot-b"])
    assert registry.add_to_room("ch-1", "bot-c") == ["bot-a", "bot-b", "bot-c"]
    assert registry.room_members("ch-1") == ["bot-a", "bot-b", "bot-c"]
    assert registry.remove_from_room("ch-1", "bot-a") is True
    assert registry.room_members("ch-1") == ["bot-b", "bot-c"]


def test_room_cap_enforced_on_create(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="6"):
        registry.create_room("ch-1", [f"bot-{i}" for i in range(7)])
    assert registry.room_members("ch-1") == []


def test_room_cap_enforced_on_add(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.create_room("ch-1", [f"bot-{i}" for i in range(6)])
    with pytest.raises(ValueError, match="full"):
        registry.add_to_room("ch-1", "bot-7")
    assert len(registry.room_members("ch-1")) == 6


def test_bind_channel_refuses_a_room_channel(tmp_path, monkeypatch):
    """XOR, binding side: a channel is a room OR a single-bot binding, never
    both — binding a room channel would silently orphan its members."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.create_room("ch-1", ["bot-a", "bot-b"])
    with pytest.raises(ValueError, match="room"):
        registry.bind_channel("ch-1", profile="bot-c", blueprint="reminders")
    # the refusal left both sides untouched
    assert registry.get_binding("ch-1") is None
    assert registry.room_members("ch-1") == ["bot-a", "bot-b"]


def test_create_room_refuses_a_bound_channel(tmp_path, monkeypatch):
    """XOR, room side."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.bind_channel("ch-1", profile="bot-a", blueprint="reminders")
    with pytest.raises(ValueError, match="bound"):
        registry.create_room("ch-1", ["bot-b", "bot-c"])
    assert registry.room_members("ch-1") == []
    assert registry.get_binding("ch-1")["profile"] == "bot-a"


def test_add_to_room_refuses_a_bound_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.bind_channel("ch-1", profile="bot-a", blueprint="reminders")
    with pytest.raises(ValueError, match="bound"):
        registry.add_to_room("ch-1", "bot-b")


def test_removing_the_last_member_deletes_the_room(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.create_room("ch-1", ["bot-a"])
    assert registry.remove_from_room("ch-1", "bot-a") is True
    assert registry.room_members("ch-1") == []
    raw = json.loads((tmp_path / "concierge" / "registry.json").read_text())
    assert "ch-1" not in raw["rooms"]
    # the channel is free to become a single-bot binding afterwards
    registry.bind_channel("ch-1", profile="bot-a", blueprint="reminders")
    assert registry.get_binding("ch-1")["profile"] == "bot-a"


def test_room_members_unknown_channel_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert registry.room_members("nope") == []


def test_registry_written_before_rooms_loads_and_takes_rooms(tmp_path, monkeypatch):
    """Back-compat: a registry.json written before rooms existed (no "rooms"
    key) loads fine, keeps its bindings, and accepts new rooms."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cdir = tmp_path / "concierge"
    cdir.mkdir()
    (cdir / "registry.json").write_text(json.dumps(
        {"version": 1,
         "bindings": {"100": {"profile": "bot-a", "blueprint": "reminders"}}}))
    assert registry.room_members("100") == []  # a binding, not a room
    registry.create_room("200", ["bot-b", "bot-c"])  # rooms work on the old file
    assert registry.get_binding("100")["profile"] == "bot-a"  # bindings survived
    assert registry.room_members("200") == ["bot-b", "bot-c"]


def test_duplicate_member_add_is_refused(tmp_path, monkeypatch):
    """Decision: duplicates raise ValueError rather than being silently
    idempotent — a stale picker or double-tap must surface, not pretend the
    add worked."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.create_room("ch-1", ["bot-a", "bot-b"])
    with pytest.raises(ValueError, match="already a member"):
        registry.add_to_room("ch-1", "bot-a")
    assert registry.room_members("ch-1") == ["bot-a", "bot-b"]


def test_create_room_refuses_duplicate_and_empty_members(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="duplicate"):
        registry.create_room("ch-1", ["bot-a", "bot-a"])
    with pytest.raises(ValueError, match="at least one member"):
        registry.create_room("ch-1", [])
    assert registry.room_members("ch-1") == []


def test_add_to_room_unknown_channel_is_refused(tmp_path, monkeypatch):
    """Decision: add_to_room never creates a room implicitly — create_room is
    the only creation path, so a stray add is a caller bug, loudly reported."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="not a bot room"):
        registry.add_to_room("ch-1", "bot-a")


def test_remove_from_room_no_ops_return_false(tmp_path, monkeypatch):
    """Mirrors unbind_channel's silent-False: removing a non-member, or
    removing from a non-room channel, is a no-op — never a crash."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.create_room("ch-1", ["bot-a"])
    assert registry.remove_from_room("ch-1", "bot-z") is False
    assert registry.remove_from_room("nope", "bot-a") is False
    assert registry.room_members("ch-1") == ["bot-a"]


def test_recreating_a_room_replaces_membership(tmp_path, monkeypatch):
    """Mirrors bind_channel's idempotent rebind-overwrite: create_room over an
    existing room resets its membership (the /room recreate flow, Task 7.4)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.create_room("ch-1", ["bot-a", "bot-b"])
    registry.create_room("ch-1", ["bot-c", "bot-d"])
    assert registry.room_members("ch-1") == ["bot-c", "bot-d"]


# ── M7a review fix: write-path member validation ────────────────────────────


def test_create_room_refuses_blank_and_non_string_members(tmp_path, monkeypatch):
    """The read/decision layers filter blank/non-string members defensively,
    so the WRITE path must refuse them — otherwise ``create_room('ch',
    ['  '])`` stores a member that is INVISIBLE to room_members (fan-out never
    runs it) while still XOR-blocking bind_channel forever."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="non-blank string"):
        registry.create_room("ch-1", ["  "])
    with pytest.raises(ValueError, match="non-blank string"):
        registry.create_room("ch-1", ["bot-a", None])
    with pytest.raises(ValueError, match="non-blank string"):
        registry.create_room("ch-1", [42])
    # refused means refused: nothing was stored
    assert registry.room_members("ch-1") == []


def test_create_room_refuses_whitespace_padded_members(tmp_path, monkeypatch):
    """Decision: a padded name (``'bot-a '``) is refused, NOT silently
    stripped — the duplicate-member error style is to surface caller bugs,
    never to repair the caller's data. A strip-and-store would hide the very
    bug that produced the padding."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="whitespace"):
        registry.create_room("ch-1", ["bot-a ", "bot-b"])
    assert registry.room_members("ch-1") == []


def test_add_to_room_refuses_blank_and_non_string_members(tmp_path, monkeypatch):
    """Same contract on the add path: ``add_to_room('ch', None)`` used to
    persist None — invisible to room_members yet counting toward the
    6-member cap. The write path refuses it, file untouched."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.create_room("ch-1", ["bot-a"])
    with pytest.raises(ValueError, match="non-blank string"):
        registry.add_to_room("ch-1", None)
    with pytest.raises(ValueError, match="non-blank string"):
        registry.add_to_room("ch-1", "  ")
    with pytest.raises(ValueError, match="whitespace"):
        registry.add_to_room("ch-1", "bot-b\t")
    assert registry.room_members("ch-1") == ["bot-a"]


def test_junk_room_values_are_skipped_not_fatal(tmp_path, monkeypatch):
    """Same tolerance as bindings: one hand-edited junk room entry must not
    cost the whole registry; the next save drops it (self-healing)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cdir = tmp_path / "concierge"
    cdir.mkdir()
    (cdir / "registry.json").write_text(json.dumps({
        "version": 1, "bindings": {},
        "rooms": {"bad-str": "junk", "bad-list": ["junk"],
                  "good": {"members": ["bot-a", "bot-b"], "created_at": "x"}}}))
    assert registry.room_members("bad-str") == []
    assert registry.room_members("bad-list") == []
    assert registry.room_members("good") == ["bot-a", "bot-b"]
    registry.create_room("fresh", ["bot-c"])
    raw = json.loads((cdir / "registry.json").read_text())
    assert sorted(raw["rooms"]) == ["fresh", "good"]


# ── Task 8.2: connection-nudge state (survives restarts, on the binding) ────


def test_mark_and_get_nudged_connections_round_trip(tmp_path, monkeypatch):
    """NUDGE-STATE DECISION (picked + documented): the once-flag lives ON
    THE REGISTRY BINDING as ``nudged_connections`` — registry persistence,
    not in-memory — so a gateway RESTART never re-nudges a user who already
    saw the message. Stored as the list of kinds already nudged (once per
    (binding, kind), not once per binding): a re-provision with the SAME
    blueprint keeps the binding and its nudged set; a newly required kind
    still gets exactly one nudge."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.bind_channel("ch-1", profile="bot-scheduler-1",
                          blueprint="scheduler", guild_id="42")

    assert registry.nudged_connections("ch-1") == []
    merged = registry.mark_connections_nudged("ch-1", ["calendar"])
    assert merged == ["calendar"]

    # persisted in the registry file, on the binding itself
    raw = json.loads(
        (tmp_path / "concierge" / "registry.json").read_text())
    assert raw["bindings"]["ch-1"]["nudged_connections"] == ["calendar"]
    assert registry.nudged_connections("ch-1") == ["calendar"]

    # a second mark MERGES (order-preserving, deduped), never replaces
    merged = registry.mark_connections_nudged(
        "ch-1", ["email", "calendar"])
    assert merged == ["calendar", "email"]
    assert registry.nudged_connections("ch-1") == ["calendar", "email"]


def test_nudged_connections_absent_or_unknown_channel_is_empty(
        tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.bind_channel("ch-1", profile="bot-a", blueprint="reminders")
    # a binding written before M8 has no key: reads as never-nudged
    assert registry.nudged_connections("ch-1") == []
    assert registry.nudged_connections("nope") == []


def test_nudged_connections_tolerates_hand_edited_junk(tmp_path, monkeypatch):
    """A non-list or non-string entries read as empty — the registry's
    one-shared-tolerance-policy posture, never a crash on the hot path."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cdir = tmp_path / "concierge"
    cdir.mkdir()
    (cdir / "registry.json").write_text(json.dumps({
        "version": 1,
        "bindings": {"str-junk": {"profile": "bot-a", "blueprint": "scheduler",
                                  "nudged_connections": "calendar"},
                     "mixed-junk": {"profile": "bot-b",
                                    "blueprint": "scheduler",
                                    "nudged_connections": ["calendar", 7]}}}))
    assert registry.nudged_connections("str-junk") == []
    assert registry.nudged_connections("mixed-junk") == ["calendar"]


def test_mark_nudged_connections_is_a_noop_for_unknown_channel(
        tmp_path, monkeypatch):
    """Unbound/removed under us (retire raced the nudge): nothing to mark,
    never a write that resurrects a binding."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert registry.mark_connections_nudged("nope", ["calendar"]) == []
    assert not (tmp_path / "concierge" / "registry.json").exists()


def test_nudged_state_resets_on_a_different_blueprint_rebind(
        tmp_path, monkeypatch):
    """``bind_channel`` replaces the whole binding (its documented
    idempotent-rebind-overwrite contract), so a nudged set does not survive
    a REBIND. That is the correct end-to-end semantics because the two
    real rebind paths are asymmetric: a same-blueprint re-provision never
    reaches ``bind_channel`` (provision's ``create_bot`` returns the
    existing binding first — pinned in test_provision.py — so the flag
    survives), while a DIFFERENT blueprint retires the old bot and binds
    fresh — and the new bot's prerequisites are new, so a fresh nudge set
    is exactly right."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.bind_channel("ch-1", profile="bot-scheduler-1",
                          blueprint="scheduler", guild_id="42")
    registry.mark_connections_nudged("ch-1", ["calendar"])
    assert registry.nudged_connections("ch-1") == ["calendar"]
    # a rebind (what a different-blueprint re-provision ends in) is a
    # fresh binding: the once-flag resets with it
    registry.bind_channel("ch-1", profile="bot-email-2",
                          blueprint="email_triage", guild_id="42")
    assert registry.nudged_connections("ch-1") == []
