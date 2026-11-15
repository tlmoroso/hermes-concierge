"""Synthetic lane identity for the Discord concierge mixin.

A dispatch / room lane is a SYNTHETIC turn: no inbound Discord message exists for
it. Core (``gateway/session_authority.py::_admit_native``) mints EVERY native
admission's ``request_id`` straight from ``event.message_id``::

    request_id = str(payload['native_text_v1']['event']['message_id'])

A lane cloned with ``dataclasses.replace(event, ...)`` inherits the trigger's
``message_id``; re-admitting it into the SAME (principal, target session) as the
original but with a DIFFERENT payload (the task text / channel_context / routing
verdict) trips ``admission_conflict`` in
``hermes_state_runtime.admit_session_input`` -- the live private-home-channel bug
(a follow-up looked routed to the bot AND raised RuntimeStoreError).

Each synthetic lane therefore needs its OWN ``message_id`` (its own request
identity), unique per (request, target session, routing decision). The reply
anchor stays pinned to the real trigger via ``reply_anchor_override`` -- the
field ``_reply_anchor_for_event`` honours over ``message_id`` -- so replies still
quote the user's message exactly as before the id change.
"""

import itertools

_dispatch_lane_seq = itertools.count(1)


def _concierge_lane_message_id() -> str:
    """Fresh numeric-string id for one synthetic concierge lane event.

    A future-snowflake base keeps it in the same shape as the fetch anchor and
    clear of any real (already-arrived) Discord id; the monotonic counter makes
    it unique even when two lanes are minted in the same clock tick.
    """
    # Late import: the facade owns the anchor and imports this sibling at module
    # level, so a module-level import back would cycle.
    from plugins.platforms.discord.concierge_mixin import _dispatch_fetch_anchor_id
    return str(_dispatch_fetch_anchor_id() + next(_dispatch_lane_seq))


def _concierge_lane_identity(event) -> dict:
    """``dataclasses.replace`` kwargs giving a lane clone its own request id
    while preserving the trigger's reply anchor byte-for-byte."""
    anchor = (getattr(event, "reply_anchor_override", None)
              or getattr(event, "message_id", None))
    return {"message_id": _concierge_lane_message_id(),
            "reply_anchor_override": anchor}
