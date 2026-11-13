"""Registry users section (orchestrator redesign, T1): per-user orchestrator
and worker records — round-trip, idempotence, v1 back-compat, hand-edit
tolerance, and survival across the existing binding/room mutation paths."""
import json
import logging

import pytest

from concierge import registry


def test_set_user_round_trip_and_persisted_shape(tmp_path, monkeypatch):
    """set_user persists a per-user entry under ``users`` with the locked
    schema; the file stays version 1 (additive, the rooms precedent)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    entry = registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2",
                              home_channel="ch-1", guild_id="42")
    assert entry["orchestrator"] == "bot-orchestrator-a1b2"
    assert entry["home_channel"] == "ch-1"
    assert entry["guild_id"] == "42"
    assert entry["created"]
    assert entry["workers"] == {}
    raw = json.loads((tmp_path / "concierge" / "registry.json").read_text())
    assert raw["version"] == 1
    assert raw["users"]["u-1"]["orchestrator"] == "bot-orchestrator-a1b2"
    assert raw["users"]["u-1"]["home_channel"] == "ch-1"
    assert raw["users"]["u-1"]["guild_id"] == "42"
    assert raw["users"]["u-1"]["workers"] == {}
    # the response-only already_provisioned flag never reaches the file
    assert "already_provisioned" not in raw["users"]["u-1"]


def test_set_user_is_idempotent_for_the_same_orchestrator(tmp_path, monkeypatch):
    """Same-orchestrator set_user returns the EXISTING entry annotated
    ``already_provisioned: True`` (create_bot's truth-in-advertising
    precedent) — response metadata only, the stored entry is untouched."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    first = registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2",
                              home_channel="ch-1", guild_id="42")
    second = registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2")
    assert second["already_provisioned"] is True
    assert second["created"] == first["created"]
    assert second["home_channel"] == "ch-1"  # the stored entry, not the args
    assert "already_provisioned" not in registry.get_user("u-1")
    raw = json.loads((tmp_path / "concierge" / "registry.json").read_text())
    assert "already_provisioned" not in raw["users"]["u-1"]


def test_set_user_with_a_different_orchestrator_overwrites(tmp_path, monkeypatch):
    """Reprovision parity with bind_channel's rebind-overwrite: a DIFFERENT
    orchestrator profile replaces the entry instead of no-oping."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2",
                      home_channel="ch-1", guild_id="42")
    replaced = registry.set_user("u-1", orchestrator="bot-orchestrator-f9e8")
    assert replaced["orchestrator"] == "bot-orchestrator-f9e8"
    assert "already_provisioned" not in replaced
    assert registry.orchestrator_for_user("u-1") == "bot-orchestrator-f9e8"


def test_get_user_returns_a_copy_or_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert registry.get_user("nope") is None
    registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2")
    entry = registry.get_user("u-1")
    assert entry["orchestrator"] == "bot-orchestrator-a1b2"
    entry["orchestrator"] = "hand-mangled"  # a caller mutating the copy
    assert registry.get_user("u-1")["orchestrator"] == "bot-orchestrator-a1b2"


def test_list_users_returns_a_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert registry.list_users() == {}
    registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2")
    registry.set_user("u-2", orchestrator="bot-orchestrator-c3d4")
    users = registry.list_users()
    assert sorted(users) == ["u-1", "u-2"]
    users["u-1"]["orchestrator"] = "hand-mangled"
    assert registry.orchestrator_for_user("u-1") == "bot-orchestrator-a1b2"


def test_update_user_merges_known_fields_only(tmp_path, monkeypatch):
    """update_user merge-updates home_channel/guild_id; unknown user IDs are
    a no-op returning None (update never resurrects a removed entry)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2")
    updated = registry.update_user("u-1", home_channel="ch-9", guild_id="99")
    assert updated["home_channel"] == "ch-9"
    assert updated["guild_id"] == "99"
    # the merge preserved every untouched field
    assert updated["orchestrator"] == "bot-orchestrator-a1b2"
    assert updated["workers"] == {}
    stored = registry.get_user("u-1")
    assert stored["home_channel"] == "ch-9" and stored["guild_id"] == "99"
    # unknown user: no-op, never a write that resurrects an entry
    assert registry.update_user("nope", home_channel="ch-9") is None
    assert registry.get_user("nope") is None


def test_update_user_refuses_unknown_fields(tmp_path, monkeypatch):
    """Merge-update is closed over the known scalar fields — a typo'd field
    must surface (the write path's surface-caller-bugs style), not silently
    drop the update the caller thought they made."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2")
    with pytest.raises(ValueError, match="unknown user fields"):
        registry.update_user("u-1", home_chanel="ch-9")
    assert registry.get_user("u-1").get("home_chanel") is None


def test_user_accessors_read_the_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # everything reads None/empty before any user exists
    assert registry.orchestrator_for_user("u-1") is None
    assert registry.home_channel_for_user("u-1") is None
    assert registry.worker_for_user("u-1", "researcher") is None
    registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2",
                      home_channel="ch-1", guild_id="42")
    assert registry.orchestrator_for_user("u-1") == "bot-orchestrator-a1b2"
    assert registry.home_channel_for_user("u-1") == "ch-1"
    # no worker recorded yet
    assert registry.worker_for_user("u-1", "researcher") is None


def test_set_user_worker_round_trip_and_idempotence(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2")
    entry = registry.set_user_worker("u-1", "researcher", "bot-researcher-x1")
    assert entry["workers"] == {"researcher": "bot-researcher-x1"}
    raw = json.loads((tmp_path / "concierge" / "registry.json").read_text())
    assert raw["users"]["u-1"]["workers"] == {"researcher": "bot-researcher-x1"}
    assert registry.worker_for_user("u-1", "researcher") == "bot-researcher-x1"
    # same profile for the same key: idempotent no-op, entry returned
    again = registry.set_user_worker("u-1", "researcher", "bot-researcher-x1")
    assert again["workers"] == {"researcher": "bot-researcher-x1"}
    # a different profile for the same key overwrites
    over = registry.set_user_worker("u-1", "researcher", "bot-researcher-x2")
    assert over["workers"] == {"researcher": "bot-researcher-x2"}
    assert registry.worker_for_user("u-1", "researcher") == "bot-researcher-x2"
    # a second blueprint key coexists
    registry.set_user_worker("u-1", "notes", "bot-notes-y7")
    assert registry.worker_for_user("u-1", "notes") == "bot-notes-y7"
    assert registry.worker_for_user("u-1", "researcher") == "bot-researcher-x2"
    # unknown user: no-op returning None, never an implicit set_user
    assert registry.set_user_worker("nope", "researcher", "bot-r") is None
    assert registry.get_user("nope") is None


def test_remove_user_and_remove_user_worker(tmp_path, monkeypatch):
    """Removals return True when something was removed, False otherwise —
    the retire paths key off this contract."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2")
    registry.set_user_worker("u-1", "researcher", "bot-researcher-x1")
    assert registry.remove_user_worker("u-1", "notes") is False
    assert registry.remove_user_worker("u-1", "researcher") is True
    assert registry.remove_user_worker("u-1", "researcher") is False
    assert registry.worker_for_user("u-1", "researcher") is None
    # the user entry itself survives a worker removal
    assert registry.orchestrator_for_user("u-1") == "bot-orchestrator-a1b2"
    assert registry.remove_user("u-1") is True
    assert registry.remove_user("u-1") is False
    assert registry.get_user("u-1") is None
    raw = json.loads((tmp_path / "concierge" / "registry.json").read_text())
    assert "u-1" not in raw["users"]


def test_registry_written_before_users_loads_with_empty_users(
        tmp_path, monkeypatch):
    """Back-compat: a v1 registry (no ``users`` key) loads fine, keeps its
    bindings, and reads users as {} — the pre-rooms precedent."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cdir = tmp_path / "concierge"
    cdir.mkdir()
    (cdir / "registry.json").write_text(json.dumps(
        {"version": 1,
         "bindings": {"100": {"profile": "bot-a", "blueprint": "reminders"}}}))
    assert registry.load_registry()["users"] == {}
    assert registry.list_users() == {}
    # users work on the old file, and the binding survived
    registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2")
    assert registry.get_binding("100")["profile"] == "bot-a"
    assert registry.orchestrator_for_user("u-1") == "bot-orchestrator-a1b2"


def test_fresh_registry_save_includes_empty_users(tmp_path, monkeypatch):
    """_empty_registry carries a users section, so the first save of a fresh
    install already has the key (load_registry never has to guess)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.bind_channel("ch-1", profile="bot-a", blueprint="reminders")
    raw = json.loads((tmp_path / "concierge" / "registry.json").read_text())
    assert raw["users"] == {}
    assert raw["version"] == 1


def test_users_survive_the_existing_mutation_paths(tmp_path, monkeypatch):
    """bind/unbind/create_room/remove_from_room/mark_connections_nudged all
    load-modify-save the WHOLE registry dict, so a users section must ride
    along untouched — the section must never strand on any legacy path."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2",
                      home_channel="ch-1", guild_id="42")
    registry.set_user_worker("u-1", "researcher", "bot-researcher-x1")
    before = json.loads(
        (tmp_path / "concierge" / "registry.json").read_text())["users"]

    registry.bind_channel("ch-1", profile="bot-a", blueprint="reminders",
                          guild_id="42")
    registry.mark_connections_nudged("ch-1", ["calendar"])
    registry.create_room("ch-2", ["bot-b", "bot-c"])
    registry.remove_from_room("ch-2", "bot-b")
    registry.unbind_channel("ch-1")

    after = json.loads(
        (tmp_path / "concierge" / "registry.json").read_text())["users"]
    assert after == before
    assert registry.orchestrator_for_user("u-1") == "bot-orchestrator-a1b2"
    assert registry.worker_for_user("u-1", "researcher") == "bot-researcher-x1"


def test_partial_user_entries_read_as_defaults(tmp_path, monkeypatch):
    """Tolerant reads: a hand-edited entry missing workers/home_channel/
    guild_id reads as {}/None/None — never a crash on the hot path."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cdir = tmp_path / "concierge"
    cdir.mkdir()
    (cdir / "registry.json").write_text(json.dumps({
        "version": 1, "bindings": {},
        "users": {"u-1": {"orchestrator": "bot-orchestrator-a1b2"},
                  "u-2": {"orchestrator": "bot-orchestrator-c3d4",
                          "workers": "junk-not-a-dict",
                          "home_channel": 42}}}))
    entry = registry.get_user("u-1")
    assert entry["workers"] == {}
    assert entry["home_channel"] is None
    assert entry["guild_id"] is None
    assert registry.home_channel_for_user("u-1") is None
    assert registry.worker_for_user("u-1", "researcher") is None
    # junk VALUES inside an otherwise-valid entry read as defaults too
    junk = registry.get_user("u-2")
    assert junk["workers"] == {}
    assert junk["home_channel"] is None
    assert registry.orchestrator_for_user("u-2") == "bot-orchestrator-c3d4"


def test_junk_user_entries_are_skipped_with_a_logged_reason(
        tmp_path, monkeypatch, caplog):
    """Non-dict user VALUES are skipped by the same _clean_section policy as
    bindings/rooms (one bad entry must not cost the registry) with a logged
    reason; the next save drops them (self-healing)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cdir = tmp_path / "concierge"
    cdir.mkdir()
    (cdir / "registry.json").write_text(json.dumps({
        "version": 1, "bindings": {},
        "users": {"bad-str": "junk", "bad-list": ["junk"],
                  "u-1": {"orchestrator": "bot-orchestrator-a1b2"}}}))
    with caplog.at_level(logging.WARNING, logger="concierge.registry"):
        assert registry.get_user("bad-str") is None
        assert registry.get_user("bad-list") is None
        assert registry.get_user("u-1")["orchestrator"] == "bot-orchestrator-a1b2"
    assert "skipping 2 non-dict user entries" in caplog.text
    # a subsequent mutation persists only the valid entries
    registry.set_user("u-2", orchestrator="bot-orchestrator-f9e8")
    raw = json.loads((cdir / "registry.json").read_text())
    assert sorted(raw["users"]) == ["u-1", "u-2"]


def test_channel_less_workers_resolve_via_the_users_section(
        tmp_path, monkeypatch):
    """Regression (orchestrator redesign): channels_for_profile legally
    returns [] for a channel-less per-user bot — the users section, not the
    bindings, is where that bot's profile resolves."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2")
    registry.set_user_worker("u-1", "researcher", "bot-researcher-x1")
    # the worker has NO channel binding — and that is fine
    assert registry.channels_for_profile("bot-researcher-x1") == []
    assert registry.worker_for_user("u-1", "researcher") == "bot-researcher-x1"
    # the orchestrator itself is channel-less until a home channel is bound
    assert registry.channels_for_profile("bot-orchestrator-a1b2") == []
    assert registry.orchestrator_for_user("u-1") == "bot-orchestrator-a1b2"


# ---------------------------------------------------------------------------
# T1 quality-review ride-alongs (folded into T3): idempotent-path
# normalization, ignored idempotent args, write-path validation, id shapes
# ---------------------------------------------------------------------------

def test_set_user_idempotent_path_returns_a_normalized_copy(
        tmp_path, monkeypatch):
    """The idempotent early return must run through _normalize_user_entry: a
    hand-edited junk ``workers`` value (or non-string home_channel) inside an
    otherwise-valid entry must never reach the caller raw — the same
    tolerance policy every other read applies."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cdir = tmp_path / "concierge"
    cdir.mkdir()
    (cdir / "registry.json").write_text(json.dumps({
        "version": 1, "bindings": {},
        "users": {"u-1": {"orchestrator": "bot-orchestrator-a1b2",
                          "workers": "junk-not-a-dict",
                          "home_channel": 42}}}))
    result = registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2")
    assert result["already_provisioned"] is True
    assert result["workers"] == {}  # normalized, never the raw junk
    assert result["home_channel"] is None
    assert result["orchestrator"] == "bot-orchestrator-a1b2"
    # the stored entry itself is untouched (response metadata only)
    raw = json.loads((cdir / "registry.json").read_text())
    assert raw["users"]["u-1"]["workers"] == "junk-not-a-dict"


def test_set_user_idempotent_path_ignores_differing_scalar_args(
        tmp_path, monkeypatch):
    """On the idempotent path a differing ``home_channel``/``guild_id`` is
    intentionally IGNORED — the stored entry wins (only a DIFFERENT
    orchestrator profile is a reprovision; new scalar values never are)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2",
                      home_channel="ch-1", guild_id="42")
    again = registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2",
                              home_channel="ch-OTHER", guild_id="OTHER")
    assert again["already_provisioned"] is True
    assert again["home_channel"] == "ch-1"  # stored entry, not the args
    assert again["guild_id"] == "42"
    stored = registry.get_user("u-1")
    assert stored["home_channel"] == "ch-1" and stored["guild_id"] == "42"


def test_set_user_refuses_a_blank_orchestrator(tmp_path, monkeypatch):
    """_require_profile_string on the write path: a blank orchestrator
    raises and writes nothing — a stored blank would be invisible to every
    tolerant reader."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="non-blank string"):
        registry.set_user("u-1", orchestrator="   ")
    assert registry.get_user("u-1") is None


def test_set_user_refuses_a_non_string_home_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="home_channel"):
        registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2",
                          home_channel=42)
    assert registry.get_user("u-1") is None  # refused before any write


def test_update_user_refuses_a_non_string_home_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2")
    with pytest.raises(ValueError, match="home_channel"):
        registry.update_user("u-1", home_channel=42)
    assert registry.home_channel_for_user("u-1") is None  # nothing merged


def test_user_id_key_normalizes_int_and_str_shapes(tmp_path, monkeypatch):
    """Discord user ids arrive as both ints and strings depending on the
    transport; the registry keys by ``str(user_id)`` so an int-shaped write
    and a str-shaped read (and vice versa) resolve to the SAME entry."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # int-shaped write
    registry.set_user(12345, orchestrator="bot-orchestrator-a1b2",
                      home_channel="ch-1")
    raw = json.loads(
        (tmp_path / "concierge" / "registry.json").read_text())
    assert "12345" in raw["users"]  # stored key is the string form
    # str-shaped reads of the int-shaped write
    assert registry.get_user("12345")["orchestrator"] == "bot-orchestrator-a1b2"
    assert registry.orchestrator_for_user("12345") == "bot-orchestrator-a1b2"
    assert registry.home_channel_for_user("12345") == "ch-1"
    # str-shaped mutation of the int-keyed entry
    registry.set_user_worker("12345", "researcher", "bot-researcher-x1")
    assert registry.worker_for_user(12345, "researcher") == "bot-researcher-x1"
    # and the reverse shape: str write, int read
    registry.set_user("678", orchestrator="bot-orchestrator-c3d4")
    assert registry.get_user(678)["orchestrator"] == "bot-orchestrator-c3d4"
    # removals accept either shape too
    assert registry.remove_user_worker("12345", "researcher") is True
    assert registry.remove_user(12345) is True
    assert registry.get_user("12345") is None
    assert registry.get_user(678) is not None


# ---------------------------------------------------------------------------
# T3 quality fix: targeted orchestrator clear — a swap/retire must not wipe
# the entry's worker records (their profiles stay alive)
# ---------------------------------------------------------------------------

def test_clear_orchestrator_is_targeted(tmp_path, monkeypatch):
    """clear_orchestrator clears ONLY the entry's orchestrator + home_channel:
    ``workers`` (whose profiles stay alive when an orchestrator is retired or
    swapped), ``guild_id`` and ``created`` all survive — a wiped worker record
    is the exact double-build the user-keyed idempotence exists to prevent.
    When the cleared entry has neither an orchestrator nor workers, it is
    removed entirely (no empty shells in the file)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # no entry at all: nothing to clear
    assert registry.clear_orchestrator("u-1") is False
    registry.set_user("u-1", orchestrator="bot-orchestrator-a1b2",
                      home_channel="ch-1", guild_id="42")
    registry.set_user_worker("u-1", "researcher", "bot-researcher-x1")
    assert registry.clear_orchestrator("u-1") is True
    entry = registry.get_user("u-1")
    assert entry.get("orchestrator") is None
    assert entry["home_channel"] is None
    assert entry["guild_id"] == "42"
    assert entry["created"]
    assert entry["workers"] == {"researcher": "bot-researcher-x1"}
    # a workers-only entry has nothing orchestrator-shaped to clear — but it
    # SURVIVES (the worker profiles are alive and owned)
    assert registry.clear_orchestrator("u-1") is False
    assert registry.worker_for_user("u-1", "researcher") == "bot-researcher-x1"
    # once the workers are gone too, the clear removes the empty shell
    registry.remove_user_worker("u-1", "researcher")
    assert registry.clear_orchestrator("u-1") is True
    assert registry.get_user("u-1") is None
    raw = json.loads((tmp_path / "concierge" / "registry.json").read_text())
    assert "u-1" not in raw["users"]
