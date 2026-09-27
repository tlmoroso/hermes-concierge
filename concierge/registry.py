"""Channel->bot bindings: <hermes_home>/concierge/registry.json.

Plain atomic JSON (utils.atomic_json_write) guarded the same way cron/jobs.py
guards jobs.json: an in-process lock for parallel threads PLUS a cross-process
advisory flock on <concierge dir>/.registry.lock, so a `hermes concierge` CLI
write can never be clobbered by a concurrent gateway bind/unbind. A corrupt
file is never silently overwritten: it is preserved as registry.json.corrupt,
logged, and the registry starts empty. Shape:

    {"version": 1,
     "bindings": {"<channel_id>": {
         "profile": "bot-reminders-a1b2", "blueprint": "reminders",
         "guild_id": "...", "provider_group": null, "created_at": "..."}},
     "rooms": {"<channel_id>": {
         "members": ["bot-reminders-a1b2", "bot-scheduler-c3d4"],
         "created_at": "..."}}}

A ROOM is one channel bound to SEVERAL member profiles (Task 7.1; desktop
Bot Mode parity, capped at MAX_ROOM_MEMBERS). A channel is a room XOR a
single-bot binding — BOTH mutation sides refuse the cross (bind_channel
raises on a room channel, create_room/add_to_room raise on a bound one) so
neither side can silently strand the other's routing. Rooms ride the same
file, the same flock and the same corruption machinery as bindings; a
registry written before rooms existed (no "rooms" key) loads fine.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now
from utils import atomic_json_write

# Cross-process advisory locking for registry.json: fcntl (Unix) or msvcrt
# (Windows). If both are absent, _registry_lock() degrades to in-process
# locking rather than failing (mirrors cron/jobs.py).
try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None

logger = logging.getLogger(__name__)

__all__ = ["registry_path", "load_registry", "bind_channel", "unbind_channel",
           "get_binding", "channels_for_profile", "list_bindings",
           "MAX_ROOM_MEMBERS", "create_room", "room_members",
           "add_to_room", "remove_from_room",
           "nudged_connections", "mark_connections_nudged"]

REGISTRY_VERSION = 1

# Room membership cap — desktop Bot Mode parity (website/docs/user-guide/
# bot-mode.md § Groups: rooms hold 2–6 members). Enforced on create_room AND
# add_to_room; the /room picker (Task 7.4) enforces the same bound at the UX
# layer, the registry is the authority.
MAX_ROOM_MEMBERS = 6

# In-process once-latch for the unreadable-registry WARNING (M5b re-review
# carry-forward): the Discord hot path calls load_registry() per inbound event,
# so a persistently unreadable file must not log a full traceback per message.
# Keyed by registry path so a test that repoints HERMES_HOME is a fresh
# incident; cleared by nothing because the fix (repair the file/permissions)
# removes the condition itself.
_unreadable_warned: set = set()

# In-process lock for load_registry->modify->_save cycles; without it, parallel
# threads' bind/unbind calls clobber each other. RLock + thread-local depth so
# nested _registry_lock() sections in one thread reuse the held lock (cron parity).
_lock = threading.RLock()
_lock_state = threading.local()

# Upper bound on waiting for the cross-process .registry.lock. Every registry
# mutation funnels through _registry_lock(), so blocking forever on a wedged
# sibling process would freeze every bind/unbind (and with them the Discord
# surface). Mirrors cron's _JOBS_LOCK_TIMEOUT_SECONDS rationale.
_REGISTRY_LOCK_TIMEOUT_SECONDS = 30.0


def registry_path() -> Path:
    return get_hermes_home() / "concierge" / "registry.json"


def _registry_lock_file() -> Path:
    """Advisory lock path for the active registry directory — resolved alongside
    the registry itself so a patched HERMES_HOME never lands on a literal
    ``~/.hermes`` path."""
    return registry_path().parent / ".registry.lock"


def _empty_registry() -> Dict:
    return {"version": REGISTRY_VERSION, "bindings": {}, "rooms": {}}


def _acquire_flock(lock_fd, timeout: float) -> Optional[bool]:
    """Bounded exclusive lock: True when acquired, False on timeout, None when no
    backend exists. A blocking flock(LOCK_EX) taken under the in-process lock
    would let a wedged sibling freeze EVERY registry mutation forever, so poll
    LOCK_NB against a deadline; the caller picks the degraded mode (cron parity)."""
    if fcntl is not None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except (OSError, IOError):
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.1)
    if msvcrt is not None:
        getattr(msvcrt, "locking")(lock_fd.fileno(), getattr(msvcrt, "LK_LOCK"), 1)
        return True
    return None


def _release_flock(lock_fd) -> None:
    """Unlock (best effort) and close a lock file opened for ``_acquire_flock``."""
    try:
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        elif msvcrt is not None:
            getattr(msvcrt, "locking")(lock_fd.fileno(), getattr(msvcrt, "LK_UNLCK"), 1)
    except (OSError, IOError):
        pass
    finally:
        lock_fd.close()


@contextlib.contextmanager
def _registry_lock():
    """Serialize a load_registry->modify->_save critical section: in-process RLock
    (parallel threads) plus a cross-process flock on ``<concierge
    dir>/.registry.lock`` (gateway vs. CLI writes — otherwise a CLI unbind could
    be clobbered and the binding keep routing). Nested calls in one thread reuse
    the held lock. Without a flock backend, or on flock timeout (logged loudly),
    it degrades to in-process-only locking: a briefly torn cross-process write
    beats a dead registry (mirrors cron/jobs.py::_jobs_lock)."""
    depth = getattr(_lock_state, "depth", 0)
    if depth:
        _lock_state.depth = depth + 1
        try:
            yield
        finally:
            _lock_state.depth -= 1
        return

    with _lock:
        _lock_state.depth = 1
        lock_fd = None
        try:
            try:
                lock_path = _registry_lock_file()
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                lock_fd = open(lock_path, "a+", encoding="utf-8")
                lock_fd.seek(0)
                if _acquire_flock(lock_fd, _REGISTRY_LOCK_TIMEOUT_SECONDS) is False:
                    logger.error(
                        "Timed out after %.0fs waiting for the concierge registry "
                        "lock (%s) — another process is holding it. Proceeding with "
                        "in-process locking only so the registry stays usable.",
                        _REGISTRY_LOCK_TIMEOUT_SECONDS, lock_path)
                    with contextlib.suppress(OSError):
                        lock_fd.close()
                    lock_fd = None
            except (OSError, IOError) as e:
                # A locking failure must never take down registry writes — the
                # in-process lock is still held.
                logger.warning("registry.json cross-process lock unavailable (%s); "
                               "proceeding with in-process lock only", e)
            try:
                yield
            finally:
                if lock_fd is not None:
                    _release_flock(lock_fd)
        finally:
            _lock_state.depth = 0


def _preserve_corrupt_registry(path: Path, reason) -> None:
    """Copy a corrupt registry aside before the next save overwrites it
    (hermes_cli/auth.py precedent): never silently destroy the user's bindings,
    and never advertise a backup that was not written."""
    corrupt_path = path.with_suffix(".json.corrupt")
    try:
        shutil.copy2(path, corrupt_path)
        preserved = True
    except Exception:
        preserved = False
        logger.debug("concierge registry: could not preserve a copy of the corrupt "
                     "registry at %s", corrupt_path, exc_info=True)
    logger.warning(
        "concierge registry: failed to load %s (%s), starting with an empty "
        "registry. %s %s",
        path, reason,
        "Corrupt file preserved at" if preserved
        else "A copy could NOT be preserved at",
        corrupt_path)


def _warn_registry_unreadable_once(path) -> None:
    """Emit the unreadable-registry WARNING (with traceback) once per path per
    process; silent after. The load still RAISES (the caller degrades to the
    hub / empty-bindings posture) — this only caps the log cardinality."""
    key = str(path)
    if key in _unreadable_warned:
        return
    _unreadable_warned.add(key)
    logger.warning(
        "concierge registry: could not read %s, leaving the file on disk "
        "untouched rather than degrading to an empty registry",
        path, exc_info=True)


def _clean_section(raw: Dict, path: Path, key: str, singular: str) -> bool:
    """Normalize a top-level ``key`` holding a channel->entry mapping
    (``bindings``/``rooms`` — one shared tolerance policy, not two copies):

    - missing key -> ``{}`` (benign: a registry written before the key existed
      — back-compat for pre-rooms files on the ``rooms`` side);
    - a non-dict CONTAINER is corruption: preserve the file aside and return
      False so the caller degrades to an empty registry (never a silent
      partial load that one save would overwrite);
    - non-dict entry VALUES (hand edits, partial writes) are skipped with a
      logged reason instead of crashing every reader — one bad entry must not
      cost the whole registry (cron's id-keyed-map precedent). The next save
      drops them, so the file self-heals.
    """
    section = raw.get(key)
    if section is None:
        raw[key] = {}
        return True
    if not isinstance(section, dict):
        _preserve_corrupt_registry(
            path, f"'{key}' is {type(section).__name__}, expected an object")
        return False
    skipped = [cid for cid, entry in section.items() if not isinstance(entry, dict)]
    if skipped:
        logger.warning(
            "concierge registry: skipping %d non-dict %s%s in %s: %s",
            len(skipped), singular, "" if len(skipped) == 1 else "s", path,
            ", ".join(map(repr, skipped)))
        raw[key] = {cid: entry for cid, entry in section.items()
                    if isinstance(entry, dict)}
    return True


def load_registry() -> Dict:
    path = registry_path()
    if not path.exists():
        return _empty_registry()
    try:
        # utf-8-sig absorbs a BOM (cron's _parse_jobs_file parity): an
        # editor-saved file must not read as corruption.
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError:
        # Exists but unreadable (EMFILE, EACCES, EIO, stalled mount): the contents
        # are not bad, and this module read-modify-writes, so an empty registry
        # here is one _save() away from erasing every binding. Leave the file on
        # disk untouched and fail loudly (hermes_cli/auth.py precedent).
        #
        # M5b re-review carry-forward: this WARNING is DEDUPED per file path.
        # Every inbound Discord message in an enabled-concierge install calls
        # list_bindings() -> load_registry(), so the un-deduped form logged the
        # full traceback once per message, forever, on the hot path.
        _warn_registry_unreadable_once(path)
        raise
    except Exception as exc:
        # Genuine corruption: unparseable JSON or non-UTF-8 bytes (the
        # UnicodeDecodeError path — a ValueError, invisible to a bare
        # ``except (json.JSONDecodeError, OSError)``).
        _preserve_corrupt_registry(path, exc)
        return _empty_registry()
    if not isinstance(raw, dict):
        _preserve_corrupt_registry(
            path, f"top level is {type(raw).__name__}, expected an object")
        return _empty_registry()
    if not _clean_section(raw, path, "bindings", "binding"):
        return _empty_registry()
    if not _clean_section(raw, path, "rooms", "room"):
        return _empty_registry()
    raw.setdefault("version", REGISTRY_VERSION)
    return raw


def _save(reg: Dict) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, reg)


def bind_channel(channel_id: str, *, profile: str, blueprint: str,
                 guild_id: Optional[str] = None,
                 provider_group: Optional[str] = None) -> Dict:
    """Bind a Discord channel to a bot profile (idempotent: rebind overwrites).

    Refuses (ValueError) a channel that is already a ROOM: a channel is a
    single-bot binding XOR a room, and binding a room channel would silently
    strand its members' routing. Remove the room's members first — the last
    removal disbands it (remove_from_room).
    """
    with _registry_lock():
        reg = load_registry()
        cid = str(channel_id)
        room = reg["rooms"].get(cid)
        if room is not None:
            count = len(_stored_members(room))
            raise ValueError(
                f"channel {cid} is already a bot room ({count} member"
                f"{'s' if count != 1 else ''}); remove_from_room its members "
                f"(the last removal disbands it) before binding a single bot")
        binding = {"profile": profile, "blueprint": blueprint, "guild_id": guild_id,
                   "provider_group": provider_group,
                   "created_at": _hermes_now().isoformat()}
        reg["bindings"][cid] = binding
        _save(reg)
        return dict(binding)


def unbind_channel(channel_id: str) -> bool:
    with _registry_lock():
        reg = load_registry()
        if str(channel_id) not in reg["bindings"]:
            return False
        del reg["bindings"][str(channel_id)]
        _save(reg)
        return True


def get_binding(channel_id: str) -> Optional[Dict]:
    binding = load_registry()["bindings"].get(str(channel_id))
    return dict(binding) if isinstance(binding, dict) else None


def channels_for_profile(profile: str) -> List[str]:
    reg = load_registry()
    return [cid for cid, b in reg["bindings"].items() if b.get("profile") == profile]


def list_bindings() -> Dict[str, Dict]:
    return dict(load_registry()["bindings"])


# ── Rooms (Task 7.1): one channel, several member profiles ────────────────


def _stored_members(room: Dict) -> List:
    """A room's members list, defensively: load_registry only guarantees the
    ENTRY is a dict, so a hand-edited members value must not crash a mutation
    (iterating a junk string would silently char-split it)."""
    stored = room.get("members")
    return list(stored) if isinstance(stored, list) else []


def _require_member_string(member) -> None:
    """Write-path member validation (M7a review fix): the read/decision layers
    (room_members, resolve_room_targets) filter blank/non-string entries
    defensively, so a member the WRITE path accepts must already be one they
    would keep — otherwise three layers disagree on "what is a member" and a
    stored blank/None entry is invisible to fan-out while still XOR-blocking
    bind_channel and counting toward the cap. Whitespace-padded names are
    refused too, NOT silently stripped: the duplicate-member error style is to
    surface caller bugs, never to repair the caller's data."""
    if not isinstance(member, str) or not member.strip():
        raise ValueError(
            f"room members must be non-blank strings; got {member!r}")
    if member != member.strip():
        raise ValueError(
            f"room member {member!r} has surrounding whitespace; pass clean "
            f"profile names")


def _normalize_member_list(members) -> List[str]:
    """Consume any iterable of profile names into a stored-order list,
    enforcing the invariants that apply to ANY whole-membership write
    (create_room): every entry is a non-blank, unpadded string
    (_require_member_string), at least one member (an empty room is an
    invisible wedge — room_members would read [] exactly like "not a room"
    while still blocking bind_channel through the XOR), no duplicates (a stale
    picker or double-tap must surface, not silently no-op), and the
    MAX_ROOM_MEMBERS cap."""
    member_list = list(members)
    if not member_list:
        raise ValueError("a room needs at least one member profile")
    seen = set()
    for member in member_list:
        _require_member_string(member)
        if member in seen:
            raise ValueError(f"duplicate room member {member!r}: {member_list!r}")
        seen.add(member)
    if len(member_list) > MAX_ROOM_MEMBERS:
        raise ValueError(
            f"a room holds at most {MAX_ROOM_MEMBERS} members; got "
            f"{len(member_list)}: {member_list!r}")
    return member_list


def create_room(channel_id: str, members) -> Dict:
    """Make ``channel_id`` a bot room holding ``members`` (any iterable of
    profile names); returns the stored room dict.

    The stored iteration order IS the room's canonical member order — the
    fan-out order for un-addressed messages (routing.resolve_room_targets).
    Refusals (ValueError, file untouched): a channel already bound as a
    single-bot channel (the XOR, room side — unbind first); duplicate or zero
    members; more than MAX_ROOM_MEMBERS. Creating a room over an existing room
    REPLACES its membership — mirrors bind_channel's idempotent
    rebind-overwrite (the /room recreate flow, Task 7.4).
    """
    member_list = _normalize_member_list(members)  # pure validation, pre-lock
    with _registry_lock():
        reg = load_registry()
        cid = str(channel_id)
        binding = reg["bindings"].get(cid)
        if binding is not None:
            raise ValueError(
                f"channel {cid} is already bound to single-bot profile "
                f"{binding.get('profile')!r}; unbind_channel({cid!r}) before "
                f"creating a room")
        room = {"members": member_list,
                "created_at": _hermes_now().isoformat()}
        reg["rooms"][cid] = room
        _save(reg)
        return dict(room)


def room_members(channel_id: str) -> List[str]:
    """The room's member profiles in stored (creation/add) order; [] when the
    channel is not a room (or the entry is hand-edited junk). This list feeds
    routing.resolve_room_targets — the adapter's "is this channel a room?"
    lookup."""
    room = load_registry()["rooms"].get(str(channel_id))
    if not isinstance(room, dict):
        return []
    return [m for m in _stored_members(room) if isinstance(m, str)]


def add_to_room(channel_id: str, profile: str) -> List[str]:
    """Add ``profile`` to the room on ``channel_id``; returns the new member
    list (the member goes LAST — stored order is creation/add order).

    Refusals (ValueError, file untouched): a channel bound as a single-bot
    channel (the XOR); a channel that is not a room at all (create_room is the
    only creation path — a stray add is a caller bug, loudly reported, never
    an implicit one-member room); a member that is not a non-blank, unpadded
    string (write-path validation, _require_member_string); a duplicate member
    (surfaced, not silently idempotent); a full room (MAX_ROOM_MEMBERS).
    """
    _require_member_string(profile)  # pure validation, pre-lock
    with _registry_lock():
        reg = load_registry()
        cid = str(channel_id)
        binding = reg["bindings"].get(cid)
        if binding is not None:
            raise ValueError(
                f"channel {cid} is already bound to single-bot profile "
                f"{binding.get('profile')!r}; a bound channel cannot take room "
                f"members")
        room = reg["rooms"].get(cid)
        if not isinstance(room, dict):
            raise ValueError(
                f"channel {cid} is not a bot room; create_room({cid!r}, [...]) "
                f"it first")
        members = _stored_members(room)
        if profile in members:
            raise ValueError(
                f"profile {profile!r} is already a member of the room on "
                f"channel {cid}")
        if len(members) >= MAX_ROOM_MEMBERS:
            raise ValueError(
                f"the room on channel {cid} is full ({MAX_ROOM_MEMBERS} members "
                f"max); remove a member before adding another")
        members.append(profile)
        room["members"] = members
        _save(reg)
        return list(members)


def remove_from_room(channel_id: str, profile: str) -> bool:
    """Remove ``profile`` from the room on ``channel_id``.

    Mirrors unbind_channel's contract: True when a member was removed, False
    when there was nothing to remove (not a room, or not a member) — never a
    crash. Removing the LAST member deletes the room entirely: a zero-member
    room is an invisible wedge (see _normalize_member_list), and the channel
    becomes bindable again.
    """
    with _registry_lock():
        reg = load_registry()
        cid = str(channel_id)
        room = reg["rooms"].get(cid)
        if not isinstance(room, dict):
            return False
        members = _stored_members(room)
        if profile not in members:
            return False
        members.remove(profile)
        if members:
            room["members"] = members
        else:
            del reg["rooms"][cid]  # last member out: the room is gone
        _save(reg)
        return True


# ── Task 8.2: connection-nudge state on the binding ────────────────────────


def _binding_nudged(binding: Dict) -> List[str]:
    """A binding's nudged kinds, defensively: a hand-edited value must not
    crash the hot path (the registry's one tolerance policy — non-list
    reads as never-nudged, junk entries drop out, the next save
    self-heals)."""
    stored = binding.get("nudged_connections")
    if not isinstance(stored, list):
        return []
    return [k for k in stored if isinstance(k, str) and k.strip()]


def nudged_connections(channel_id: str) -> List[str]:
    """The account kinds this channel's binding has ALREADY nudged about
    (Task 8.2 once-flag, registry-persisted so a gateway restart never
    re-nudges). ``[]`` when unbound, or for a binding written before the
    key existed (back-compat: reads as never-nudged, which nudges exactly
    once more and then persists the key)."""
    binding = load_registry()["bindings"].get(str(channel_id))
    if not isinstance(binding, dict):
        return []
    return _binding_nudged(binding)


def mark_connections_nudged(channel_id: str, kinds) -> List[str]:
    """Record that the binding on *channel_id* nudged about *kinds* — MERGE
    into the stored list (order-preserving, deduped), never replace: the
    once-flag is per (binding, kind), so a newly required kind still gets
    exactly one nudge while nudged kinds stay quiet.

    A no-op (``[]``) for an unbound/removed channel: marking never
    resurrects a binding (a retire can race the nudge). Returns the merged
    list actually stored, or ``[]`` when there was no binding to mark.
    """
    new_kinds = [str(k) for k in (kinds or [])
                 if isinstance(k, str) and k.strip()]
    with _registry_lock():
        reg = load_registry()
        binding = reg["bindings"].get(str(channel_id))
        if not isinstance(binding, dict):
            return []
        merged = _binding_nudged(binding)
        for kind in new_kinds:
            if kind not in merged:
                merged.append(kind)
        binding["nudged_connections"] = merged
        _save(reg)
        return list(merged)
