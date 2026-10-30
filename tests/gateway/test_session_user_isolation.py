"""Invariant: distinct Discord users never share a session key in a shared channel."""

from gateway.session import SessionSource, Platform, build_session_key


def _src(user_id, chat_id="C1", chat_type="channel"):
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
    )


def test_distinct_users_get_distinct_keys_in_channel():
    k1 = build_session_key(_src("U1"), group_sessions_per_user=True)
    k2 = build_session_key(_src("U2"), group_sessions_per_user=True)
    assert k1 != k2


def test_same_user_same_key():
    k1 = build_session_key(_src("U1"))
    k2 = build_session_key(_src("U1"))
    assert k1 == k2


def test_dm_keys_differ_by_user():
    k1 = build_session_key(_src("U1", chat_id="DM_U1", chat_type="dm"))
    k2 = build_session_key(_src("U2", chat_id="DM_U2", chat_type="dm"))
    assert k1 != k2