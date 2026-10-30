import os

import pytest

from concierge.auth_tiers import (
    admin_ids,
    chat_user_ids,
    is_admin,
    is_admitted,
    is_chat_only,
)


def test_admin_defaults_to_allowed_users(monkeypatch):
    monkeypatch.delenv("DISCORD_ADMIN_USERS", raising=False)
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "111,222")
    monkeypatch.delenv("DISCORD_USER_USERS", raising=False)
    assert admin_ids() == {"111", "222"}
    assert is_admin("111") is True
    assert is_admin("999") is False


def test_chat_only_users(monkeypatch):
    monkeypatch.setenv("DISCORD_ADMIN_USERS", "111")
    monkeypatch.setenv("DISCORD_USER_USERS", "333,444")
    assert chat_user_ids() == {"333", "444"}
    assert is_chat_only("333") is True
    assert is_chat_only("111") is False  # admin, not chat-only
    assert is_admitted("333") is True
    assert is_admitted("999") is False


def test_mention_format_stripped(monkeypatch):
    monkeypatch.setenv("DISCORD_ADMIN_USERS", "<@12345>")
    assert admin_ids() == {"12345"}
    assert is_admin("12345") is True