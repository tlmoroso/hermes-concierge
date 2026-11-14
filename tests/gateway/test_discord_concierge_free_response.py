"""Concierge bound/room channels are free-response: no @mention needed.

Live bug (2026-09-27 E2E): ``_handle_message``'s require-mention gate
(``DISCORD_REQUIRE_MENTION`` defaults true) silently drops guild messages
that do not @mention the bot unless the channel is free-response. A
provisioned bot OWNS its channel — requiring an @mention of the shared hub
identity there makes every bound bot unreachable. The adapter's
``_discord_free_response_channels`` now unions the concierge registry's
bound + room channels through a thin hook; these tests drive the REAL
``_handle_message`` gate (the one the live message died at) against a temp
registry.

Managed-category free-response (2026-11-14 live E2E bug): the orchestrator
front door's channels are UNBOUND channels inside a concierge MANAGED
category — the id-based hook above never listed them, so the mention gate
dropped plain typing before the concierge wrap (and the T4 bootstrap behind
it) ever saw it. The new message-shaped hook supersedes the union at the
three gate sites: "a channel in a concierge managed category is
free-response". Thread channels don't resolve a category, so a thread in a
managed category stays mention-gated (the T4-documented fail-closed
limitation). A message mentioning another BOT is dropped earlier and
unconditionally by admission's anti-ping-pong guard (adapter ~1847,
channel-independent) — the mentions-others site reachable here is the
third-party (human) mention one.

Rig mirrors tests/gateway/test_discord_free_response.py: a real
DiscordAdapter over the conftest discord mock with ``handle_message``
await-mocked at the gateway boundary.
"""

from datetime import datetime, timezone, UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from concierge import registry
from gateway.config import PlatformConfig

from tests.gateway.conftest import _ensure_discord_mock

_ensure_discord_mock()

import plugins.platforms.discord.adapter as discord_platform
from plugins.platforms.discord.adapter import DiscordAdapter

BOUND_CHANNEL = 1553803778493382688
ROOM_CHANNEL = 1553809999999999999
UNBOUND_CHANNEL = 7
MANAGED_CATEGORY = 500
UNMANAGED_CATEGORY = 600
OTHER_USER_ID = 820


class FakeTextChannel:
    def __init__(self, channel_id: int, name: str = "chan",
                 category_id: int | None = None):
        self.id = channel_id
        self.name = name
        self.parent_id = None
        self.category_id = category_id


def make_message(*, channel, content: str, mentions=None):
    author = SimpleNamespace(id=42, display_name="Tyler", name="Tyler", bot=False)
    return SimpleNamespace(
        id=123,
        content=content,
        mentions=[] if mentions is None else mentions,
        attachments=[],
        reference=None,
        created_at=datetime.now(UTC),
        channel=channel,
        author=author,
        type=discord_platform.discord.MessageType.default,
        guild=SimpleNamespace(id=1),
    )


@pytest.fixture
def adapter(monkeypatch):
    # Plain marker classes: the stub channel must NOT be an instance of either
    # (isinstance(channel, discord.Thread/DMChannel) drives thread/DM exemptions
    # in _handle_message — making FakeTextChannel BE discord.Thread would exempt
    # every message from the require-mention gate we are testing).
    monkeypatch.setattr(discord_platform.discord, "DMChannel",
                        type("FakeDM", (), {}), raising=False)
    monkeypatch.setattr(discord_platform.discord, "Thread",
                        type("FakeThread", (), {}), raising=False)
    for _var in (
        "DISCORD_REQUIRE_MENTION",
        "DISCORD_THREAD_REQUIRE_MENTION",
        "DISCORD_FREE_RESPONSE_CHANNELS",
        "DISCORD_FREE_RESPONSE_AUTO_THREAD",
        "DISCORD_AUTO_THREAD",
        "DISCORD_NO_THREAD_CHANNELS",
        "DISCORD_ALLOWED_CHANNELS",
        "DISCORD_IGNORED_CHANNELS",
        "DISCORD_IGNORE_NO_MENTION",
    ):
        monkeypatch.delenv(_var, raising=False)

    config = PlatformConfig(enabled=True, token="fake-token")
    a = DiscordAdapter(config)
    a._client = SimpleNamespace(user=SimpleNamespace(id=999, bot=True))
    a._text_batch_delay_seconds = 0
    a._is_allowed_user = Mock(return_value=True)
    a.handle_message = AsyncMock()
    a.gateway_runner = None
    return a


def _bind(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.bind_channel(str(BOUND_CHANNEL), profile="bot-reminders-x",
                          blueprint="reminders")


def _managed_concierge_runner(adapter, *, enabled=True,
                              categories=(str(MANAGED_CATEGORY),)):
    """Point the adapter at a runner whose in-hand config carries a
    concierge section with managed categories (the same shape
    ``_concierge_settings_obj``/``_concierge_events_wanted`` read live)."""
    adapter.gateway_runner = SimpleNamespace(
        config=SimpleNamespace(
            concierge={"enabled": enabled, "hub_profile": "concierge",
                       "managed_category_ids": list(categories)},
            multiplex_profiles=True,
        ))


@pytest.mark.asyncio
async def test_bound_channel_message_without_mention_reaches_gateway(
        tmp_path, monkeypatch, adapter):
    _bind(tmp_path, monkeypatch)

    await adapter._handle_message(
        make_message(channel=FakeTextChannel(BOUND_CHANNEL),
                     content="remind me to stretch"))

    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_room_channel_message_without_mention_reaches_gateway(
        tmp_path, monkeypatch, adapter):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry.create_room(str(ROOM_CHANNEL), ["bot-a", "bot-b"])

    await adapter._handle_message(
        make_message(channel=FakeTextChannel(ROOM_CHANNEL),
                     content="what does tomorrow look like?"))

    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_unbound_channel_without_mention_still_dropped(
        tmp_path, monkeypatch, adapter):
    # The union must be registry-driven, not a blanket free pass.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    await adapter._handle_message(
        make_message(channel=FakeTextChannel(UNBOUND_CHANNEL),
                     content="remind me to stretch"))

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_free_response_env_applies_alongside_bindings(
        tmp_path, monkeypatch, adapter):
    _bind(tmp_path, monkeypatch)
    monkeypatch.setenv("DISCORD_FREE_RESPONSE_CHANNELS", "555")

    await adapter._handle_message(
        make_message(channel=FakeTextChannel(555), content="hello"))
    await adapter._handle_message(
        make_message(channel=FakeTextChannel(BOUND_CHANNEL), content="hello"))

    assert adapter.handle_message.await_count == 2


@pytest.mark.asyncio
async def test_union_survives_an_unreadable_registry(
        tmp_path, monkeypatch, adapter):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "concierge").mkdir(parents=True)
    (tmp_path / "concierge" / "registry.json").write_text("{not json")

    # Fail-open to the base set: no crash, unbound channels still dropped.
    await adapter._handle_message(
        make_message(channel=FakeTextChannel(UNBOUND_CHANNEL),
                     content="hello"))

    adapter.handle_message.assert_not_awaited()


# ── Managed-category free-response (2026-11-14 live E2E hotfix) ─────────────


@pytest.mark.asyncio
async def test_managed_category_unbound_channel_without_mention_reaches_gateway(
        tmp_path, monkeypatch, adapter):
    # The orchestrator front door: a fresh, UNBOUND channel inside a managed
    # category must answer plain typing — the id-based union never listed it.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _managed_concierge_runner(adapter)

    await adapter._handle_message(
        make_message(channel=FakeTextChannel(
                         UNBOUND_CHANNEL, category_id=MANAGED_CATEGORY),
                     content="plan my week"))

    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_managed_category_unbound_channel_mentioning_another_user_admitted(
        tmp_path, monkeypatch, adapter):
    # The admission mentions-others site: a third-party (human) mention in a
    # managed-category unbound channel must not be dropped either. A message
    # mentioning another BOT never reaches this site — admission's
    # anti-ping-pong guard drops it earlier, unconditionally and
    # channel-independent.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _managed_concierge_runner(adapter)

    admitted, _ = adapter._discord_message_admission(
        make_message(channel=FakeTextChannel(
                         UNBOUND_CHANNEL, category_id=MANAGED_CATEGORY),
                     content=f"<@{OTHER_USER_ID}> can you help?",
                     mentions=[SimpleNamespace(id=OTHER_USER_ID, bot=False,
                                               display_name="Kim", name="Kim")]),
        claim=False)

    assert admitted is True


@pytest.mark.asyncio
async def test_unmanaged_or_uncategorized_channel_without_mention_still_dropped(
        tmp_path, monkeypatch, adapter):
    # The rule is category-driven, not a blanket free pass: a category the
    # operator never claimed, or no category at all, keeps the mention gate.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _managed_concierge_runner(adapter)

    await adapter._handle_message(
        make_message(channel=FakeTextChannel(
                         UNBOUND_CHANNEL, category_id=UNMANAGED_CATEGORY),
                     content="hello"))
    adapter.handle_message.assert_not_awaited()

    await adapter._handle_message(
        make_message(channel=FakeTextChannel(UNBOUND_CHANNEL),
                     content="hello"))
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_thread_in_managed_category_without_mention_still_dropped(
        tmp_path, monkeypatch, adapter):
    # Fail-closed limitation, pinned: thread channels don't resolve a
    # category, so the hook cannot see the parent channel's managed category
    # — a thread message without a mention stays dropped. The channel IS a
    # Thread instance (the conftest-mocked discord.Thread) but is NOT one the
    # bot joined, so no thread exemption bypasses the gate either.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _managed_concierge_runner(adapter)

    # Runtime ``discord`` here is the conftest mock (a plain no-arg class);
    # Pyright sees the real discord.py Thread signature.
    thread = discord_platform.discord.Thread()  # type: ignore[call-issue]
    thread.id = 21  # type: ignore[attributeAccessIssue]
    thread.name = "thread"
    thread.parent_id = None  # type: ignore[attributeAccessIssue]

    await adapter._handle_message(make_message(channel=thread, content="hello"))

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_concierge_disabled_managed_category_channel_still_dropped(
        tmp_path, monkeypatch, adapter):
    # Zero-behavior-change rule: concierge disabled → the hook is False →
    # the mention gate is exactly what it was before the feature.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _managed_concierge_runner(adapter, enabled=False)

    await adapter._handle_message(
        make_message(channel=FakeTextChannel(
                         UNBOUND_CHANNEL, category_id=MANAGED_CATEGORY),
                     content="hello"))

    adapter.handle_message.assert_not_awaited()
