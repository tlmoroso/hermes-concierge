"""Concierge home-category card E (design §4, §5.3): thread-aware category
resolution + the in-scope gate for the unbound→orchestrator re-home.

The ROOT DEFECT: a message typed inside a THREAD in a managed category did
not reach the author's orchestrator. ``_concierge_event_category_id`` read
the category off the RAW channel only; for a thread discord.py's
``Thread.category_id`` resolves the PARENT's category only when cached and
otherwise RAISES ``ClientException``, which escaped into the wrap's
fail-closed handler and delegated the message to the hub.

The fix:
- ``_concierge_event_category_id`` resolves THROUGH the parent (``parent_id``
  → ``client.get_channel`` → parent's ``category_id``), one hop, fail-closed.
- ``_concierge_event_in_scope`` is the new gate ``_concierge_orchestrator_
  fallback_on`` uses: thread-aware managed-category check (authoritative),
  widened by the shared ``_concierge_owns_conversation`` predicate (card D)
  when present, fail-closed when it is absent (parallel card) or raises.
- ``_concierge_event_in_managed_category`` stays the raw predicate other
  callers (bootstrap/placement) depend on.

Rig: a lightweight ``_Probe(ConciergeDiscordMixin)`` hosting ONLY the pure
routing-decision helpers (stub hub client + stub settings). No full gateway /
registry needed — these helpers read the category off the raw channel, the
managed list off settings, and the parent off the client. This keeps the
suite self-contained and isolated from the sibling cards editing the big
``test_discord_concierge_orchestrator_fallback.py`` file in parallel.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from tests.gateway.conftest import _ensure_discord_mock

_ensure_discord_mock()

from plugins.platforms.discord.concierge_mixin import (
    ConciergeDiscordMixin,
    _CONCIERGE_ORCHESTRATOR_ATTR,
)

MANAGED_CATEGORY = "31337"
UNMANAGED_CATEGORY = "999"
PARENT_CHANNEL_ID = 555
THREAD_ID = 777
GRANDPARENT_ID = 8888
ORCH = "bot-orchestrator-a1b2"


# ── Duck-typed channel / client stubs ─────────────────────────────────────


class _PlainChannel:
    """A NON-thread channel: a directly-readable ``category_id``, no
    ``parent_id``. Every pre-existing routing test uses this shape."""

    def __init__(self, category_id=MANAGED_CATEGORY, channel_id=100):
        self.category_id = category_id
        self.id = channel_id


class _ParentChannel:
    """The resolved PARENT channel object. Its category may be exposed as
    ``category_id`` or (duck-typed parity) as ``category.id``."""

    def __init__(self, category_id=MANAGED_CATEGORY, *, via_category_obj=False):
        self.id = PARENT_CHANNEL_ID
        if via_category_obj:
            self.category = SimpleNamespace(id=category_id)
        else:
            self.category_id = category_id


class _GrandparentPointerChannel:
    """A parent that ITSELF looks thread-like (has ``parent_id``) but carries
    NO category — proves the resolver takes ONE hop and never walks on."""

    def __init__(self, grandparent_id=GRANDPARENT_ID):
        self.parent_id = grandparent_id
        self.id = PARENT_CHANNEL_ID  # deliberately no category_id / category


class _ThreadChannel:
    """A discord.py ``Thread``-like channel: exposes ``parent_id``; its OWN
    ``category_id`` mimics the real property — it RAISES when the parent is
    not cached (the old bug), and is the WRONG value when it is cached (the
    thread's parent is the authority, not wherever the thread object points).
    """

    def __init__(self, *, parent_id=PARENT_CHANNEL_ID, uncached=True,
                 own_category_id=UNMANAGED_CATEGORY, thread_id=THREAD_ID):
        self.parent_id = parent_id
        self.id = thread_id
        self._uncached = uncached
        self._own_category_id = own_category_id

    @property
    def category_id(self):
        if self._uncached:
            # discord.py Thread.category_id raises ClientException when the
            # parent is not cached. A direct read (the old path) blows up.
            raise RuntimeError("ClientException: parent channel not cached")
        return self._own_category_id


class _Client:
    """Stub hub client. ``get_channel`` records its (int) argument so the
    one-hop contract can be asserted, and can be told to return a parent,
    return None (uncached), or raise."""

    def __init__(self, parent=None, *, raises=False):
        self._parent = parent
        self._raises = raises
        self.calls = []

    def get_channel(self, cid):
        self.calls.append(cid)
        if self._raises:
            raise RuntimeError("get_channel failed")
        return self._parent


class _Probe(ConciergeDiscordMixin):
    """Minimal host for the pure routing-decision helpers. Stubs the hub
    client (thread parent resolution) and the concierge settings object
    (managed-category list). ``managed=None`` simulates unreadable settings.
    An ``_concierge_owns_conversation`` hook (card D) is installed by tests
    that need it — by default it is ABSENT, mirroring the parallel state.
    """

    def __init__(self, *, client=None, managed=(MANAGED_CATEGORY,)):
        self._client = client
        self._managed = managed
        # Card D's shared predicate now lives on the mixin class, so a bare
        # subclass would inherit it. Default the instance attribute to None
        # (i.e. "hook absent") so each test controls presence explicitly and
        # never silently exercises D's real registry-reading method. Tests
        # that need the hook assign a callable to this attribute.
        self._concierge_owns_conversation: Any = None

    def _concierge_settings_obj(self):
        if self._managed is None:
            return None
        return SimpleNamespace(managed_category_ids=list(self._managed))


def _event(channel, *, stamp=ORCH, is_bot=False, parent_chat_id=None):
    """A MessageEvent-shaped object whose source already carries the
    orchestrator stamp the registry-verdict tail leaves on an UNBOUND
    channel. ``stamp=None`` / ``is_bot=True`` model the short-circuit rows."""
    source = SimpleNamespace(
        is_bot=is_bot, user_id="u1", chat_id=str(THREAD_ID),
        parent_chat_id=parent_chat_id)
    setattr(source, _CONCIERGE_ORCHESTRATOR_ATTR, stamp)
    raw = (SimpleNamespace(channel=channel, id=123)
           if channel is not None else None)
    return SimpleNamespace(source=source, raw_message=raw)


def _managed_parent_client(category=MANAGED_CATEGORY, **kw):
    return _Client(_ParentChannel(category), **kw)


# ── _concierge_event_category_id: thread-aware, one hop, fail-closed ───────


def test_thread_resolves_category_through_parent():
    """THE root fix: a thread whose PARENT sits in a managed category
    resolves the PARENT's category via the client — even though the thread's
    own ``category_id`` would RAISE (uncached parent). One hop, int id."""
    client = _managed_parent_client()
    probe = _Probe(client=client)
    event = _event(_ThreadChannel(uncached=True))  # category_id raises

    assert probe._concierge_event_category_id(event) == MANAGED_CATEGORY
    assert client.calls == [PARENT_CHANNEL_ID]  # one hop, int-converted


def test_thread_parent_category_via_category_object():
    """Duck-typed parity: the parent may expose ``category.id`` rather than
    ``category_id`` — still resolves."""
    client = _Client(_ParentChannel(MANAGED_CATEGORY, via_category_obj=True))
    probe = _Probe(client=client)

    assert probe._concierge_event_category_id(
        _event(_ThreadChannel())) == MANAGED_CATEGORY


def test_thread_parent_wins_over_its_own_category_id():
    """The placement authority is the PARENT, not the thread object. A cached
    thread may report a DIFFERENT (unmanaged) category_id — the resolver must
    still return the parent's managed category."""
    client = _managed_parent_client(MANAGED_CATEGORY)
    probe = _Probe(client=client)
    thread = _ThreadChannel(uncached=False,
                            own_category_id=UNMANAGED_CATEGORY)

    assert probe._concierge_event_category_id(_event(thread)) == MANAGED_CATEGORY


def test_plain_non_thread_channel_is_unchanged():
    """Existing behavior: a non-thread channel (no parent_id) reads its
    category straight off the raw object; the client is never consulted."""
    client = _managed_parent_client(UNMANAGED_CATEGORY)
    probe = _Probe(client=client)

    assert probe._concierge_event_category_id(
        _event(_PlainChannel(MANAGED_CATEGORY))) == MANAGED_CATEGORY
    assert probe._concierge_event_category_id(
        _event(_PlainChannel(UNMANAGED_CATEGORY))) == UNMANAGED_CATEGORY
    assert probe._concierge_event_category_id(_event(None)) is None
    assert client.calls == []  # no client hop for plain channels


@pytest.mark.parametrize("client,label", [
    (None, "no client"),
    (_Client(None), "parent not cached (get_channel -> None)"),
    (_Client(None, raises=True), "get_channel raises"),
    (_Client(_GrandparentPointerChannel()), "parent has no category"),
])
def test_unresolvable_parent_fails_closed_no_raise(client, label):
    """An uncached / unresolvable / raising parent resolves to ``None`` and
    NO exception escapes — fail-closed to the hub, never a dropped message."""
    probe = _Probe(client=client)
    event = _event(_ThreadChannel(uncached=True))  # own category_id raises

    # Must return None, not raise (a raise here is the original defect).
    assert probe._concierge_event_category_id(event) is None, label


def test_bad_parent_id_fails_closed_no_raise():
    """A non-numeric parent_id (int() raises) is swallowed, fail-closed."""
    client = _managed_parent_client()
    probe = _Probe(client=client)
    event = _event(_ThreadChannel(parent_id="not-an-int", uncached=True))

    assert probe._concierge_event_category_id(event) is None


def test_one_hop_only_never_walks_to_grandparent():
    """ONE hop only: when the parent is itself thread-like (has parent_id)
    but carries no category, the resolver returns None — it must NOT call
    ``get_channel`` again for the grandparent."""
    client = _Client(_GrandparentPointerChannel())
    probe = _Probe(client=client)

    assert probe._concierge_event_category_id(
        _event(_ThreadChannel())) is None
    assert client.calls == [PARENT_CHANNEL_ID]  # exactly one hop


# ── _concierge_event_in_scope: authoritative managed check + D widening ───


def test_in_scope_managed_thread_is_authoritative():
    """The managed check runs FIRST and is authoritative: even when the
    shared hook is present and would return False (its managed half reads the
    RAW channel and is NOT thread-aware), a managed-category THREAD is in
    scope. This is precisely why the hook cannot be the primary gate."""
    client = _managed_parent_client()
    probe = _Probe(client=client)
    calls = []
    probe._concierge_owns_conversation = (
        lambda message, source: calls.append((message, source)) or False)

    assert probe._concierge_event_in_scope(_event(_ThreadChannel())) is True
    assert calls == []  # managed check short-circuited — hook not consulted


def test_in_scope_absent_hook_falls_back_to_managed_check():
    """Card D runs in parallel: with no ``_concierge_owns_conversation`` the
    gate IS the (thread-aware) managed-category check, fail-closed."""
    client = _managed_parent_client(UNMANAGED_CATEGORY)
    probe = _Probe(client=client)

    assert probe._concierge_event_in_scope(_event(_ThreadChannel())) is False


def test_in_scope_widens_when_hook_present():
    """Once card D lands, a non-managed channel the concierge OWNS (bound /
    room / home category) widens the lane — the hook receives the raw message
    and source so the gates cannot drift."""
    client = _managed_parent_client(UNMANAGED_CATEGORY)
    probe = _Probe(client=client)
    seen = []
    probe._concierge_owns_conversation = (
        lambda message, source: seen.append((message, source)) or True)
    event = _event(_PlainChannel(UNMANAGED_CATEGORY))

    assert probe._concierge_event_in_scope(event) is True
    assert seen == [(event.raw_message, event.source)]


def test_in_scope_hook_raising_fails_closed():
    """A raising shared predicate must never break the routing decision —
    fail closed to the hub."""
    client = _managed_parent_client(UNMANAGED_CATEGORY)
    probe = _Probe(client=client)

    def _boom(message, source):
        raise RuntimeError("owns_conversation exploded")

    probe._concierge_owns_conversation = _boom

    assert probe._concierge_event_in_scope(
        _event(_PlainChannel(UNMANAGED_CATEGORY))) is False


def test_in_scope_unreadable_settings_fails_closed():
    """Unreadable settings (None) resolve False, independent of the client."""
    probe = _Probe(client=_managed_parent_client(), managed=None)

    assert probe._concierge_event_in_scope(_event(_ThreadChannel())) is False


# ── _concierge_orchestrator_fallback_on: the re-home verdict ──────────────


def test_thread_managed_parent_rehomes_to_orchestrator():
    """ACCEPTANCE 1: a human message in a THREAD whose PARENT is in a managed
    category re-homes to the author's orchestrator (stubbed client + channel
    return the parent's category)."""
    probe = _Probe(client=_managed_parent_client())
    event = _event(_ThreadChannel(uncached=True),
                   parent_chat_id=str(PARENT_CHANNEL_ID))

    assert probe._concierge_orchestrator_fallback_on(event) == ORCH


def test_thread_unmanaged_parent_keeps_the_hub():
    """ACCEPTANCE 2: a thread whose parent is NOT managed fails closed to
    today's hub behavior (the stamp lands, the scope gate blocks re-home)."""
    probe = _Probe(client=_managed_parent_client(UNMANAGED_CATEGORY))
    event = _event(_ThreadChannel(uncached=True))

    assert probe._concierge_orchestrator_fallback_on(event) is None


def test_thread_unresolvable_parent_keeps_the_hub_no_raise():
    """ACCEPTANCE 3: an unresolvable (uncached) parent fails closed and NO
    raise escapes — the thread's own category_id would RAISE, but the fix
    never reads it."""
    probe = _Probe(client=_Client(None))
    event = _event(_ThreadChannel(uncached=True))

    assert probe._concierge_orchestrator_fallback_on(event) is None


def test_bot_author_and_missing_stamp_never_rehome():
    """Anti-loop / stamp precondition unchanged: bot-authored or unstamped
    sources never re-home, even in a managed-category thread."""
    probe = _Probe(client=_managed_parent_client())

    assert probe._concierge_orchestrator_fallback_on(
        _event(_ThreadChannel(), is_bot=True)) is None
    assert probe._concierge_orchestrator_fallback_on(
        _event(_ThreadChannel(), stamp=None)) is None


def test_non_thread_rehome_behavior_unchanged():
    """ACCEPTANCE 4 (existing behavior): the plain-channel lane is byte-for-
    byte unchanged — managed re-homes, unmanaged keeps the hub; the
    binding/room thread-exempt path is untouched (covered by the existing
    ``test_discord_concierge_orchestrator_fallback.py`` suite)."""
    probe = _Probe(client=_managed_parent_client())

    assert probe._concierge_orchestrator_fallback_on(
        _event(_PlainChannel(MANAGED_CATEGORY))) == ORCH
    assert probe._concierge_orchestrator_fallback_on(
        _event(_PlainChannel(UNMANAGED_CATEGORY))) is None
