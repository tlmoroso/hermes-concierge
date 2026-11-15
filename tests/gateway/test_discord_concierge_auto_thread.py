"""Card D — the surface-wide "owns conversation" predicate + auto-thread hook.

Spec: concierge/HOME_CATEGORY_DESIGN.md §5.1–5.2, §8. Drives the REAL
``DiscordAdapter`` (ConciergeDiscordMixin is in its MRO) over the conftest
discord mock, calling the two hook methods directly — the same rig style as
tests/gateway/test_discord_concierge_free_response.py.

Card C owns the adapter-side delegation; these tests pin the mixin contract
independently of that wiring. End-to-end "reply lands in the thread" is the
existing ``_auto_create_thread`` delivery path card C reuses.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import sys

import pytest

from concierge import registry
from gateway.config import PlatformConfig

from tests.gateway.conftest import _ensure_discord_mock

_ensure_discord_mock()

import plugins.platforms.discord.adapter as discord_platform
from plugins.platforms.discord.adapter import DiscordAdapter

BOUND_CHANNEL = 1001
ROOM_CHANNEL = 1002
MANAGED_CHANNEL = 1003
HOME_CHANNEL = 1004
PLAIN_CHANNEL = 1005
MANAGED_CATEGORY = 500
HOME_CATEGORY = 700          # deliberately NOT in managed_category_ids
OTHER_CATEGORY = 800
USER_ID = 42


class _MT:
    """Deterministic MessageType sentinel (avoid MagicMock __eq__ quirks)."""
    default = "default"
    reply = "reply"


class _FakeDM:
    """Marker + channel shape: isinstance(channel, discord.DMChannel) is True."""
    def __init__(self, channel_id, *, category_id=None, parent_id=None):
        self.id = channel_id
        self.name = "dm"
        self.parent_id = parent_id
        self.category_id = category_id
        self.category = (SimpleNamespace(id=category_id)
                         if category_id is not None else None)
        self.parent = None


class _FakeThread:
    """Marker + channel shape: isinstance(channel, discord.Thread) is True."""
    def __init__(self, channel_id, *, category_id=None, parent_id=None):
        self.id = channel_id
        self.name = "thread"
        self.parent_id = parent_id
        self.category_id = category_id
        self.category = (SimpleNamespace(id=category_id)
                         if category_id is not None else None)
        self.parent = None


class FakeTextChannel:
    def __init__(self, channel_id, *, name="chan", category_id=None,
                 parent_id=None):
        self.id = channel_id
        self.name = name
        self.parent_id = parent_id
        self.category_id = category_id
        self.category = (SimpleNamespace(id=category_id)
                         if category_id is not None else None)
        self.parent = None


def make_message(*, channel, content="hello", author_id=USER_ID,
                 msg_type=_MT.default):
    author = SimpleNamespace(id=author_id, display_name="Tyler",
                             name="Tyler", bot=False)
    return SimpleNamespace(
        id=123,
        content=content,
        mentions=[],
        attachments=[],
        reference=None,
        channel=channel,
        author=author,
        type=msg_type,
        guild=SimpleNamespace(id=1),
    )


@pytest.fixture
def adapter(monkeypatch):
    for _var in (
        "DISCORD_NO_THREAD_CHANNELS",
        "DISCORD_FREE_RESPONSE_AUTO_THREAD",
        "DISCORD_AUTO_THREAD",
        "DISCORD_REQUIRE_MENTION",
        "DISCORD_FREE_RESPONSE_CHANNELS",
        "DISCORD_ALLOWED_CHANNELS",
        "DISCORD_IGNORED_CHANNELS",
    ):
        monkeypatch.delenv(_var, raising=False)

    config = PlatformConfig(enabled=True, token="fake-token")
    a = DiscordAdapter(config)
    a._client = SimpleNamespace(user=SimpleNamespace(id=999, bot=True))
    a._text_batch_delay_seconds = 0
    a._is_allowed_user = Mock(return_value=True)
    a.handle_message = AsyncMock()
    a._voice_text_channels = {}
    a.gateway_runner = None

    # The conftest mock can be re-installed by other collected test modules,
    # leaving adapter.py's ``discord`` reference and sys.modules pointing at
    # DIFFERENT mock objects. The hook resolves its markers with a lazy
    # ``import discord`` (=> sys.modules), so align sys.modules to the object
    # adapter.py imported, then install the markers on that SAME object. This
    # keeps the exclusion tests isolation-proof regardless of collection order.
    real_discord = discord_platform.discord
    monkeypatch.setitem(sys.modules, "discord", real_discord)
    monkeypatch.setattr(real_discord, "DMChannel", _FakeDM, raising=False)
    monkeypatch.setattr(real_discord, "Thread", _FakeThread, raising=False)
    monkeypatch.setattr(real_discord, "MessageType", _MT, raising=False)

    # Deterministic duck-typed adapter dependencies (the hook reads them via
    # getattr(self, ...)); tests override .return_value per case.
    a._discord_free_response_auto_thread = Mock(return_value=False)
    a._get_no_thread_channels = Mock(return_value=set())
    a._discord_channel_keys = Mock(
        side_effect=lambda message, parent=None: {
            str(getattr(message.channel, "id", ""))})
    return a


def configure(adapter, *, mode="mentions", managed=(), enabled=True,
              multiplex=True, include_threading=True):
    """Point the adapter at a runner whose in-hand concierge config carries a
    threading section and managed-category list (the shape the readers use)."""
    concierge = {
        "enabled": enabled,
        "hub_profile": "concierge",
        "managed_category_ids": [str(c) for c in managed],
    }
    if include_threading:
        concierge["threading"] = {"mode": mode}
    adapter.gateway_runner = SimpleNamespace(
        config=SimpleNamespace(concierge=concierge,
                               multiplex_profiles=multiplex))


def set_registry(monkeypatch, *, bindings=None, rooms=None, users=None,
                 home_accessor=None, broken=False):
    """Install a controlled registry read. By default force the fallback
    (no home_category_for_user accessor) so behaviour is deterministic
    whether or not card B has landed; home_accessor exercises that path."""
    state = {
        "bindings": dict(bindings or {}),
        "rooms": dict(rooms or {}),
        "users": dict(users or {}),
    }
    if broken:
        def _boom():
            raise RuntimeError("registry unreadable")
        monkeypatch.setattr(registry, "load_registry", _boom, raising=False)
    else:
        monkeypatch.setattr(registry, "load_registry", lambda: state,
                            raising=False)
    if home_accessor is None:
        monkeypatch.delattr(registry, "home_category_for_user", raising=False)
    else:
        monkeypatch.setattr(registry, "home_category_for_user", home_accessor,
                            raising=False)
    return state


# ── hook is actually reachable on the adapter (card C's getattr contract) ──

def test_hook_and_predicate_are_present_on_the_adapter(adapter):
    assert callable(getattr(adapter, "_concierge_owns_conversation", None))
    assert callable(getattr(adapter, "_concierge_wants_auto_thread", None))


# ── _concierge_owns_conversation: the ONE surface predicate ────────────────

@pytest.mark.parametrize("kind", ["bound", "room", "managed", "home"])
def test_owns_conversation_each_owned_channel_kind(adapter, monkeypatch, kind):
    configure(adapter, managed=(str(MANAGED_CATEGORY),))
    if kind == "bound":
        set_registry(monkeypatch, bindings={str(BOUND_CHANNEL): {"profile": "b"}})
        channel = FakeTextChannel(BOUND_CHANNEL)
    elif kind == "room":
        set_registry(monkeypatch, rooms={str(ROOM_CHANNEL): {"members": ["a"]}})
        channel = FakeTextChannel(ROOM_CHANNEL)
    elif kind == "managed":
        set_registry(monkeypatch)
        channel = FakeTextChannel(MANAGED_CHANNEL,
                                  category_id=MANAGED_CATEGORY)
    else:  # home — category NOT listed in managed_category_ids
        set_registry(monkeypatch,
                     users={str(USER_ID): {"home_category": str(HOME_CATEGORY)}})
        channel = FakeTextChannel(HOME_CHANNEL, category_id=HOME_CATEGORY)
    assert adapter._concierge_owns_conversation(
        make_message(channel=channel), None) is True


def test_owns_conversation_home_category_via_accessor(adapter, monkeypatch):
    """Card B's canonical home_category_for_user accessor, when present, is
    the source of truth — even with no home_category field in the users dict."""
    configure(adapter, managed=(str(MANAGED_CATEGORY),))
    set_registry(monkeypatch,
                 home_accessor=lambda uid: str(HOME_CATEGORY))
    channel = FakeTextChannel(HOME_CHANNEL, category_id=HOME_CATEGORY)
    assert adapter._concierge_owns_conversation(
        make_message(channel=channel), None) is True


def test_owns_conversation_thread_inherits_parent_binding(adapter, monkeypatch):
    """Threads/forum posts inherit the parent channel's registry key."""
    configure(adapter)
    set_registry(monkeypatch, bindings={str(BOUND_CHANNEL): {"profile": "b"}})
    channel = FakeTextChannel(9999, parent_id=BOUND_CHANNEL)
    assert adapter._concierge_owns_conversation(
        make_message(channel=channel), None) is True


def test_owns_conversation_accepts_a_built_source(adapter, monkeypatch):
    """The built-source path (card E) supplies chat_id/parent_chat_id/user_id
    instead of relying on the raw message channel."""
    configure(adapter, managed=(str(MANAGED_CATEGORY),))
    set_registry(monkeypatch, bindings={str(BOUND_CHANNEL): {"profile": "b"}})
    source = SimpleNamespace(chat_id=str(BOUND_CHANNEL),
                             parent_chat_id=None, user_id=str(USER_ID))
    assert adapter._concierge_owns_conversation(
        make_message(channel=FakeTextChannel(1)), source) is True


def test_owns_conversation_plain_channel_is_false(adapter, monkeypatch):
    configure(adapter, managed=(str(MANAGED_CATEGORY),))
    set_registry(monkeypatch)
    channel = FakeTextChannel(PLAIN_CHANNEL, category_id=OTHER_CATEGORY)
    assert adapter._concierge_owns_conversation(
        make_message(channel=channel), None) is False


def test_owns_conversation_home_for_a_different_user_is_false(adapter, monkeypatch):
    """Per-user isolation: another author's home category does not own it."""
    configure(adapter, managed=(str(MANAGED_CATEGORY),))
    set_registry(monkeypatch,
                 users={str(USER_ID): {"home_category": str(HOME_CATEGORY)}})
    channel = FakeTextChannel(HOME_CHANNEL, category_id=HOME_CATEGORY)
    assert adapter._concierge_owns_conversation(
        make_message(channel=channel, author_id=999), None) is False


def test_owns_conversation_fails_closed_on_unreadable_registry(
        adapter, monkeypatch):
    configure(adapter, managed=(str(MANAGED_CATEGORY),))
    set_registry(monkeypatch, broken=True)
    channel = FakeTextChannel(MANAGED_CHANNEL, category_id=MANAGED_CATEGORY)
    assert adapter._concierge_owns_conversation(
        make_message(channel=channel), None) is False


# ── _concierge_wants_auto_thread: off ──────────────────────────────────────
# NOTE (cross-card integration, card C owns adapter.py): these assert the HOOK's
# own contract (False -> veto). Card C's landed _wants_auto_thread is
# positive-claim (`if hook(): return True; else: base_rule`), so a falsy hook
# return falls through to the base rule instead of vetoing: on a non-free
# @mention (base True) `off` would still thread end-to-end. Card C's own
# test_hook_false_falls_back_to_base_rule documents this. Reconciliation (in
# card C's lane): make _wants_auto_thread authoritative — `return bool(hook(...))`
# when the hook is callable, base rule ONLY when absent/raising — and update that
# one test. Card D's mixin hook is already correct and needs no change.

@pytest.mark.parametrize("is_free", [False, True])
def test_off_vetoes_even_on_mention_and_free_channels(adapter, monkeypatch,
                                                      is_free):
    configure(adapter, mode="off")
    set_registry(monkeypatch, bindings={str(BOUND_CHANNEL): {}})
    # base would be True for a mention (is_free=False); off must still veto.
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=FakeTextChannel(BOUND_CHANNEL)), is_free) is False


def test_off_beats_always_owned_channel(adapter, monkeypatch):
    configure(adapter, mode="off")
    set_registry(monkeypatch, bindings={str(BOUND_CHANNEL): {}})
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=FakeTextChannel(BOUND_CHANNEL)), True) is False


# ── _concierge_wants_auto_thread: mentions (byte-identical to today) ───────

@pytest.mark.parametrize(
    "is_free,fr_auto_thread,expected_base",
    [(False, False, True),   # @mention -> thread (today)
     (True, False, False),   # free channel -> inline reply (today)
     (True, True, True)],    # free_response_auto_thread opted in
)
def test_mentions_returns_exactly_the_base_rule(adapter, monkeypatch, is_free,
                                                fr_auto_thread, expected_base):
    configure(adapter, mode="mentions")
    adapter._discord_free_response_auto_thread.return_value = fr_auto_thread
    # Even an OWNED channel must not be forced under mentions.
    set_registry(monkeypatch, bindings={str(BOUND_CHANNEL): {}})
    result = adapter._concierge_wants_auto_thread(
        make_message(channel=FakeTextChannel(BOUND_CHANNEL)), is_free)
    assert result is expected_base


def test_disabled_concierge_returns_base_rule(adapter, monkeypatch):
    configure(adapter, mode="always", enabled=False)
    set_registry(monkeypatch, bindings={str(BOUND_CHANNEL): {}})
    adapter._discord_free_response_auto_thread.return_value = False
    # is_free=True -> base False; a disabled concierge must not force a thread.
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=FakeTextChannel(BOUND_CHANNEL)), True) is False


def test_absent_threading_config_returns_base_rule(adapter, monkeypatch):
    configure(adapter, include_threading=False)
    set_registry(monkeypatch, bindings={str(BOUND_CHANNEL): {}})
    adapter._discord_free_response_auto_thread.return_value = False
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=FakeTextChannel(BOUND_CHANNEL)), True) is False
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=FakeTextChannel(BOUND_CHANNEL)), False) is True


def test_garbage_mode_fails_closed_to_base_rule(adapter, monkeypatch):
    configure(adapter, mode="nonsense")
    set_registry(monkeypatch, bindings={str(BOUND_CHANNEL): {}})
    adapter._discord_free_response_auto_thread.return_value = False
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=FakeTextChannel(BOUND_CHANNEL)), True) is False


def test_threading_mode_reader_fallback_to_settings_object(adapter, monkeypatch):
    """Card A's ConciergeSettings.threading.mode is honoured when the raw
    config section carries no threading key (reader is single source)."""
    configure(adapter, include_threading=False)
    monkeypatch.setattr(
        adapter, "_concierge_settings_obj",
        lambda: SimpleNamespace(threading=SimpleNamespace(mode="always"),
                                threading_mode=None,
                                managed_category_ids=[]),
        raising=False)
    set_registry(monkeypatch, bindings={str(BOUND_CHANNEL): {}})
    adapter._discord_free_response_auto_thread.return_value = False
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=FakeTextChannel(BOUND_CHANNEL)), True) is True


# ── _concierge_wants_auto_thread: always (surface-wide) ────────────────────

def _owned_setup(adapter, monkeypatch, kind):
    configure(adapter, mode="always", managed=(str(MANAGED_CATEGORY),))
    if kind == "bound":
        set_registry(monkeypatch, bindings={str(BOUND_CHANNEL): {}})
        return FakeTextChannel(BOUND_CHANNEL)
    if kind == "room":
        set_registry(monkeypatch, rooms={str(ROOM_CHANNEL): {}})
        return FakeTextChannel(ROOM_CHANNEL)
    if kind == "managed":
        set_registry(monkeypatch)
        return FakeTextChannel(MANAGED_CHANNEL, category_id=MANAGED_CATEGORY)
    # home — not in managed_category_ids
    set_registry(monkeypatch,
                 users={str(USER_ID): {"home_category": str(HOME_CATEGORY)}})
    return FakeTextChannel(HOME_CHANNEL, category_id=HOME_CATEGORY)


@pytest.mark.parametrize("kind", ["bound", "room", "managed", "home"])
@pytest.mark.parametrize("is_free,fr_auto", [(True, False), (False, False)])
def test_always_threads_every_owned_channel(adapter, monkeypatch, kind,
                                            is_free, fr_auto):
    """base is False for is_free=True/fr=False, so a True verdict PROVES the
    always-force overrode the base rule (surface-wide, not home-only)."""
    channel = _owned_setup(adapter, monkeypatch, kind)
    adapter._discord_free_response_auto_thread.return_value = fr_auto
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=channel), is_free) is True


def test_always_on_non_owned_channel_defers_to_base(adapter, monkeypatch):
    configure(adapter, mode="always", managed=(str(MANAGED_CATEGORY),))
    set_registry(monkeypatch)
    adapter._discord_free_response_auto_thread.return_value = False
    channel = FakeTextChannel(PLAIN_CHANNEL, category_id=OTHER_CATEGORY)
    # free + fr off -> base False; always must not force a non-owned channel
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=channel), True) is False
    # a plain @mention still threads today (base True) — unchanged
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=channel), False) is True


# ── exclusions still win under always ──────────────────────────────────────

def _always_owned_baseline(adapter, monkeypatch):
    configure(adapter, mode="always")
    set_registry(monkeypatch, bindings={str(BOUND_CHANNEL): {}})
    adapter._discord_free_response_auto_thread.return_value = False
    return FakeTextChannel(BOUND_CHANNEL)


def test_always_reply_message_exclusion_wins(adapter, monkeypatch):
    ch = _always_owned_baseline(adapter, monkeypatch)
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=ch), True) is True  # baseline: forced
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=ch, msg_type=_MT.reply), True) is False


def test_always_voice_linked_channel_exclusion_wins(adapter, monkeypatch):
    ch = _always_owned_baseline(adapter, monkeypatch)
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=ch), True) is True
    adapter._voice_text_channels = {1: BOUND_CHANNEL}
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=ch), True) is False


def test_always_no_thread_channels_exclusion_wins(adapter, monkeypatch):
    ch = _always_owned_baseline(adapter, monkeypatch)
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=ch), True) is True
    adapter._get_no_thread_channels.return_value = {str(BOUND_CHANNEL)}
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=ch), True) is False


def test_always_dm_exclusion_wins(adapter, monkeypatch):
    configure(adapter, mode="always")
    set_registry(monkeypatch, bindings={str(BOUND_CHANNEL): {}})
    adapter._discord_free_response_auto_thread.return_value = False
    # plain owned channel threads; same id as a DM does not
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=FakeTextChannel(BOUND_CHANNEL)), True) is True
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=_FakeDM(BOUND_CHANNEL)), True) is False


def test_always_message_already_in_thread_never_nests(adapter, monkeypatch):
    configure(adapter, mode="always")
    set_registry(monkeypatch, bindings={str(BOUND_CHANNEL): {}})
    adapter._discord_free_response_auto_thread.return_value = False
    # plain owned channel threads; a thread channel with the same id does not
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=FakeTextChannel(BOUND_CHANNEL)), True) is True
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=_FakeThread(BOUND_CHANNEL)), True) is False


def test_hook_fails_closed_to_base_on_reader_error(adapter, monkeypatch):
    """A broken owns-conversation read never raises and never drops intake:
    the hook degrades to the base rule."""
    configure(adapter, mode="always")
    set_registry(monkeypatch, broken=True)
    adapter._discord_free_response_auto_thread.return_value = False
    # base False (free + fr off) — no exception, no spurious thread
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=FakeTextChannel(BOUND_CHANNEL)), True) is False
    # base True on @mention — still byte-identical to today
    assert adapter._concierge_wants_auto_thread(
        make_message(channel=FakeTextChannel(BOUND_CHANNEL)), False) is True
