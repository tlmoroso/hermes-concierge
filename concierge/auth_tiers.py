"""Authorization tiers for multi-user concierge deployments.

Phase 1 of MULTI_USER_PLAN.md. Three tiers, in priority order:

- admin: full-trust (provision bots, run admin commands, use terminal/file bots).
- user:  chat-only (talk to existing bots in their own private space).
- (neither): not admitted.

``DISCORD_ADMIN_USERS`` defaults to ``DISCORD_ALLOWED_USERS`` when unset, so
existing single-user deployments keep working unchanged (backward compat).
"""

from __future__ import annotations

import os


def _parse_ids(raw: str | None) -> set[str]:
    if not raw:
        return set()
    out: set[str] = set()
    for part in raw.split(","):
        v = part.strip().lstrip("<@").rstrip(">").strip()
        if v:
            out.add(v)
    return out


def admin_ids() -> set[str]:
    raw = os.getenv("DISCORD_ADMIN_USERS")
    if raw is None or not raw.strip():
        # Backward compat: admin defaults to the legacy allowed-users set.
        raw = os.getenv("DISCORD_ALLOWED_USERS", "")
    return _parse_ids(raw)


def chat_user_ids() -> set[str]:
    return _parse_ids(os.getenv("DISCORD_USER_USERS"))


def allowed_ids() -> set[str]:
    """Union of admin + chat-only ids (any admitted user)."""
    return admin_ids() | chat_user_ids() | _parse_ids(os.getenv("DISCORD_ALLOWED_USERS"))


def is_admin(user_id: str | int | None) -> bool:
    if user_id is None:
        return False
    return str(user_id) in admin_ids()


def is_chat_only(user_id: str | int | None) -> bool:
    if user_id is None:
        return False
    uid = str(user_id)
    return uid in chat_user_ids() and uid not in admin_ids()


def is_admitted(user_id: str | int | None) -> bool:
    if user_id is None:
        return False
    return str(user_id) in allowed_ids()