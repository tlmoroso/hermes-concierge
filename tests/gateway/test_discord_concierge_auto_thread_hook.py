"""Card C — the Discord auto-thread gate delegates to a concierge hook.

The adapter resolves an optional ``_concierge_wants_auto_thread`` hook at call
time (it cannot be a plain mixin override: ``DiscordAdapter``'s own body
precedes ``ConciergeDiscordMixin`` in the MRO). With no usable hook the base
rule must be byte-for-byte today's inline ``skip_thread`` decision; a hook may
only *force* threading, and every hard exclusion (DMs, no_thread_channels,
reply messages, voice-linked channels) stays in the adapter.

Reuses the message/adapter harness from test_discord_free_response.py.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import plugins.platforms.discord.adapter as discord_platform
from plugins.platforms.discord.adapter import DiscordAdapter
from tests.gateway.test_discord_free_response import (
    FakeTextChannel,
    FakeThread,
    adapter,  # pytest fixture, re-exported into this module
    make_message,
)

# The re-exported ``adapter`` fixture is injected by NAME into the tests below;
# reference it once at module scope so static analysis sees the import as used
# (each test's ``adapter`` parameter otherwise redefines it -> F811).
_ADAPTER_FIXTURE = adapter


_ABSENT = object()  # sentinel: hook attribute truly not present


# ---------------------------------------------------------------------------
# Isolated unit tests of _wants_auto_thread (unbound method + controlled self).
# These exercise the delegation contract without any mixin/config coupling.
# ---------------------------------------------------------------------------
def _wants(message, is_free_channel, *, hook=_ABSENT, free_auto_thread=False):
    fake = SimpleNamespace(
        _discord_free_response_auto_thread=lambda: free_auto_thread,
    )
    if hook is not _ABSENT:
        fake._concierge_wants_auto_thread = hook
    return DiscordAdapter._wants_auto_thread(fake, message, is_free_channel)


def test_no_hook_uses_base_rule():
    msg = object()
    # Non-free channel -> thread; free channel -> inline (core opt-in defaults
    # to false); opt-in on -> free channel threads. Exactly today's free term.
    assert _wants(msg, False) is True
    assert _wants(msg, True) is False
    assert _wants(msg, True, free_auto_thread=True) is True


def test_hook_true_forces_thread_even_on_free_channel():
    seen = []

    def hook(message, is_free_channel):
        seen.append((message, is_free_channel))
        return True

    msg = object()
    assert _wants(msg, True, hook=hook) is True
    assert seen == [(msg, True)]  # hook receives (message, is_free_channel)


def test_hook_false_falls_back_to_base_rule():
    def hook(message, is_free_channel):
        return False

    assert _wants(object(), True, hook=hook) is False
    assert _wants(object(), False, hook=hook) is True


def test_hook_raising_falls_back_to_base_rule():
    def hook(message, is_free_channel):
        raise RuntimeError("concierge down")

    # Swallowed: no exception escapes, base rule still governs.
    assert _wants(object(), True, hook=hook) is False
    assert _wants(object(), False, hook=hook) is True


# ---------------------------------------------------------------------------
# End-to-end: _handle_message's auto-thread gate honours the hook.
# ---------------------------------------------------------------------------
def _free_channel_gate(monkeypatch, channel_id="789"):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv("DISCORD_FREE_RESPONSE_CHANNELS", channel_id)
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_AUTO_THREAD", raising=False)
    monkeypatch.delenv("DISCORD_AUTO_THREAD", raising=False)  # default true


@pytest.mark.asyncio
async def test_hook_forces_thread_on_free_channel(adapter, monkeypatch):
    _free_channel_gate(monkeypatch)
    created = FakeThread(channel_id=456, name="concierge-thread")
    adapter._auto_create_thread = AsyncMock(return_value=created)

    seen = []

    def hook(message, is_free_channel):
        seen.append(is_free_channel)
        return True

    monkeypatch.setattr(adapter, "_concierge_wants_auto_thread", hook, raising=False)

    message = make_message(channel=FakeTextChannel(channel_id=789), content="thread me")
    await adapter._handle_message(message)

    assert seen == [True]
    adapter._auto_create_thread.assert_awaited_once_with(message)
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_type == "thread"
    assert event.source.chat_id == "456"


@pytest.mark.asyncio
async def test_no_hook_nonfree_mention_threads_unchanged(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_AUTO_THREAD", raising=False)
    monkeypatch.delenv("DISCORD_AUTO_THREAD", raising=False)
    # No usable hook (non-callable instance attr shadows any mixin method).
    monkeypatch.setattr(adapter, "_concierge_wants_auto_thread", None, raising=False)

    created = FakeThread(channel_id=456, name="t")
    adapter._auto_create_thread = AsyncMock(return_value=created)
    bot_user = adapter._client.user
    message = make_message(
        channel=FakeTextChannel(channel_id=123),
        content=f"<@{bot_user.id}> hello",
        mentions=[bot_user],
    )

    await adapter._handle_message(message)

    adapter._auto_create_thread.assert_awaited_once_with(message)
    assert adapter.handle_message.await_args.args[0].source.chat_type == "thread"


@pytest.mark.asyncio
async def test_no_hook_free_channel_replies_inline(adapter, monkeypatch):
    _free_channel_gate(monkeypatch)
    monkeypatch.setattr(adapter, "_concierge_wants_auto_thread", None, raising=False)
    adapter._auto_create_thread = AsyncMock()

    message = make_message(channel=FakeTextChannel(channel_id=789), content="casual chat")
    await adapter._handle_message(message)

    adapter._auto_create_thread.assert_not_awaited()
    assert adapter.handle_message.await_args.args[0].source.chat_type == "group"


@pytest.mark.asyncio
async def test_hook_raising_falls_back_and_never_breaks_intake(adapter, monkeypatch):
    _free_channel_gate(monkeypatch)

    def bad_hook(message, is_free_channel):
        raise RuntimeError("concierge down")

    monkeypatch.setattr(adapter, "_concierge_wants_auto_thread", bad_hook, raising=False)
    adapter._auto_create_thread = AsyncMock()

    message = make_message(channel=FakeTextChannel(channel_id=789), content="still handled")
    await adapter._handle_message(message)  # must not raise

    adapter._auto_create_thread.assert_not_awaited()  # base rule: inline
    adapter.handle_message.assert_awaited_once()  # intake never broken


# ---------------------------------------------------------------------------
# The hook can only ADD threading; hard exclusions stay in the adapter and
# must still win even when the hook returns True.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hook_true_respects_no_thread_channels(adapter, monkeypatch):
    _free_channel_gate(monkeypatch)
    monkeypatch.setenv("DISCORD_NO_THREAD_CHANNELS", "789")
    monkeypatch.setattr(
        adapter, "_concierge_wants_auto_thread", lambda m, f: True, raising=False
    )
    adapter._auto_create_thread = AsyncMock()

    await adapter._handle_message(
        make_message(channel=FakeTextChannel(channel_id=789), content="explicitly inline"),
    )

    adapter._auto_create_thread.assert_not_awaited()
    assert adapter.handle_message.await_args.args[0].source.chat_type == "group"


@pytest.mark.asyncio
async def test_hook_true_respects_voice_linked_channels(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv("DISCORD_FREE_RESPONSE_AUTO_THREAD", "true")
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_AUTO_THREAD", raising=False)
    monkeypatch.setattr(
        adapter, "_concierge_wants_auto_thread", lambda m, f: True, raising=False
    )
    adapter._voice_text_channels[111] = 789
    adapter._auto_create_thread = AsyncMock()

    await adapter._handle_message(
        make_message(channel=FakeTextChannel(channel_id=789), content="voice follow-up"),
    )

    adapter._auto_create_thread.assert_not_awaited()
    assert adapter.handle_message.await_args.args[0].source.chat_type == "group"


@pytest.mark.asyncio
async def test_hook_true_respects_reply_messages(adapter, monkeypatch):
    _free_channel_gate(monkeypatch)
    monkeypatch.setattr(
        adapter, "_concierge_wants_auto_thread", lambda m, f: True, raising=False
    )
    adapter._auto_create_thread = AsyncMock()

    message = make_message(
        channel=FakeTextChannel(channel_id=789),
        content="quote reply",
        msg_type=discord_platform.discord.MessageType.reply,
    )
    await adapter._handle_message(message)

    adapter._auto_create_thread.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
