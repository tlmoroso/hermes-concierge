"""Pure routing decision: which profile runs a channel's messages (Task 5.2).

``resolve_target_profile`` is the SINGLE decision function for "this channel's
messages run as that bot profile". Pure by construction — inputs are plain
values (a bindings mapping + the inbound facts), the registry touch happens in
the CALLER (the adapter seam, Task 5.3) so this module needs no filesystem and
no imports from the gateway — the concierge package must stay importable from
the CLI, the gateway and tests without dragging the world in.

Anti-loop is decided HERE, not left to admission: a bot-authored message in a
bound channel must never route to its own profile (the bot would answer itself,
forever). ``author_is_bot=True`` in a bound channel therefore degrades to the
fallback profile (the hub) with NO binding in the verdict — the pair is
already identifiable as bot-authored (``author_is_bot`` was the input), and a
binding here would invite the caller to log/drop it as if it were routed.

ROOMS (Task 7.2) fan out instead of re-homing: a room channel runs SEVERAL
member profiles from one inbound message. ``resolve_room_targets`` is that
decision — same purity contract as ``resolve_target_profile`` (the membership
arrives as a VALUE, normally ``registry.room_members(channel_id)``), and the
ADAPTER seam picks which function to call: the rooms lookup runs FIRST (a
channel is a room XOR a binding, so at most one can match), and an empty
membership falls through to ``resolve_target_profile``. The anti-loop rule is
shared verbatim: bot-authored messages never fan out either.

Thread routing: a Discord thread/forum post is part of its parent channel —
the parent's bot runs the thread (same profile, same session family). A binding
keyed by the PARENT channel id matches the thread via ``parent_channel_id``;
the thread id itself is never a registry key.

Registry shape (concierge/registry.py): ``{"<channel_id>": {"profile": ...}}``.
This module never reads the file — callers pass ``registry.get_binding``'s
result or ``registry.list_bindings()``.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional, Sequence, Union

__all__ = [
    "RoutingVerdict",
    "resolve_room_targets",
    "resolve_target_profile",
    "should_auto_offer",
]


class RoutingVerdict:
    """The outcome of one routing decision — a value object, not a promise.

    ``profile`` is the profile the message should run as (the bound bot, or
    ``fallback_profile`` when nothing binds); ``binding`` is the registry entry
    that produced it, or ``None`` when the channel is unbound (also the
    bot-authored case — see ``author_is_bot``). ``routed`` is the re-home
    answer: the verdict is ALWAYS truthy (no ``__bool__``), so never test
    ``if verdict:`` — use ``verdict.routed``.
    """

    __slots__ = ("binding", "fallback_profile", "profile")

    def __init__(self, profile: Optional[str], binding: Optional[Mapping],
                 fallback_profile: Optional[str]) -> None:
        self.profile = profile
        self.binding = binding
        self.fallback_profile = fallback_profile

    @property
    def routed(self) -> bool:
        """True when the verdict re-homes the message to a bot profile."""
        return self.profile != self.fallback_profile

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"RoutingVerdict(profile={self.profile!r}, routed={self.routed!r}, "
                f"binding={self.binding!r})")


def _channel_key(channel_id: Optional[str | int]) -> str:
    """Registry keys are strings; inbound ids can arrive int-shaped (YAML,
    mocks, test doubles). ``str()`` is the whole normalization — Discord
    snowflakes have no aliases."""
    return str(channel_id) if channel_id is not None else ""


def resolve_target_profile(
    bindings: Mapping[str, Any],
    *,
    channel_id: Optional[str | int] = None,
    fallback_profile: Optional[str] = None,
    parent_channel_id: Optional[str | int] = None,
    author_is_bot: bool = False,
) -> RoutingVerdict:
    """Decide which profile runs ``channel_id``'s messages.

    Lookup order: the channel itself, then its parent (threads/forum posts
    inherit the parent channel's bot). Unbound → ``fallback_profile`` (the hub
    profile — the adapter's active/default, normally ``None`` meaning "no
    re-home"; the caller stamps that on the source unchanged).

    ``author_is_bot`` (anti-loop): a bot-authored message in a BOUND channel
    degrades to the fallback profile — routing a bot's own message to its own
    profile would create a self-answering loop (the bot posts, the routing
    re-homes the post, the bot answers it). Admission's ``allow_bots: none``
    gate (adapter) drops these before they ever get here in production; this is
    the routing-side half of the same guarantee, enforced twice by design
    (adapter admission + this decision function).

    Bindings with a non-string or blank ``profile`` are skipped (defensive: a
    hand-edited registry must not crash routing).
    """
    def _lookup(cid) -> Optional[Mapping]:
        binding = bindings.get(_channel_key(cid))
        if not isinstance(binding, Mapping):
            return None
        profile = binding.get("profile")
        if not isinstance(profile, str) or not profile.strip():
            return None
        return binding

    binding = _lookup(channel_id) or _lookup(parent_channel_id)
    if binding is None or author_is_bot:
        # profile == fallback: the caller stamps it unchanged (routed=False).
        return RoutingVerdict(fallback_profile, None, fallback_profile)
    return RoutingVerdict(binding["profile"], binding, fallback_profile)


def _room_member_list(members: Any) -> list[str]:
    """Normalize a room membership for a fan-out decision: list/tuple input
    only (a bare string would char-split; None means "no lookup result"),
    stored order kept, blank/non-string entries skipped (a hand-edited
    registry must neither crash the decision nor run junk as a target —
    resolve_target_profile's blank-profile skip, room edition), duplicates
    collapsed (first occurrence wins)."""
    if not isinstance(members, (list, tuple)):
        return []
    seen = set()
    out = []
    for member in members:
        if isinstance(member, str) and member.strip() and member not in seen:
            seen.add(member)
            out.append(member)
    return out


def resolve_room_targets(
    members: Any,
    *,
    author_is_bot: bool,
    mentioned_profiles: Sequence[str] = (),
) -> list[str]:
    """Room fan-out decision (Task 7.2): which room members run this message?

    ``members`` is the room's membership as a VALUE (normally
    ``registry.room_members(channel_id)``) — the ADAPTER seam answers "is
    this channel a room?" with that lookup and calls this BEFORE
    ``resolve_target_profile`` (a channel is a room XOR a binding, so at most
    one applies). Returns the ordered member profiles to run; [] means "no
    member runs" (the hub handles it).

    Decisions (each documented + tested):

    - Anti-loop first, verbatim from ``resolve_target_profile``:
      bot-authored messages NEVER fan out — not even when they mention
      members (a member answering a member would loop the room). Checked
      before any mention matching.
    - No member mentioned -> ALL members, in STORED order (creation/add
      order): the broadcast case has no user-chosen order, so the registry's
      canonical order is the reply queue.
    - Members mentioned -> only those members, in MENTION order (the order
      the adapter resolved the mentions from the message — the user's
      addressing order is the reply queue they expect), NOT stored order.
      ``mentioned_profiles`` is the adapter's resolved list; this function
      only filters it against the membership, preserving its order.
    - Mentioned names that are NOT members are IGNORED, not an error: the
      adapter resolves mentions of humans, the hub, or stale members right
      alongside room members. A message whose mentions match NO member is
      therefore room-wide traffic (all members run) — @mentioning a human
      must not silence the room.
    - Duplicate mentions collapse (a member runs once). Inputs are never
      mutated; the result is always a fresh list.
    """
    if author_is_bot:
        return []
    member_list = _room_member_list(members)
    if not member_list:
        # Not a room (empty membership / junk): never a fan-out of nothing.
        return []
    member_set = set(member_list)
    targeted: list[str] = []
    for name in mentioned_profiles:
        if isinstance(name, str) and name in member_set and name not in targeted:
            targeted.append(name)
    # No mention matched a member -> the message addressed the room, not a
    # member: broadcast, stored order.
    return targeted or list(member_list)


def should_auto_offer(category_id: Optional[str | int],
                      managed_category_ids: Any) -> bool:
    """Task 5.5 decision: does a newly created channel get the blueprint picker?

    True exactly when the channel sits in one of ``concierge.managed_category_ids``
    — the configured Discord categories where "starting a channel spins up a
    bot". Pure by construction (``managed_category_ids`` is passed in, never
    read from config/files), int/str ids normalize like registry keys, and
    ``None`` (channel without a category, e.g. a voice-channel-adjacent text
    channel or a DM) is never in a managed category.

    ``managed_category_ids`` accepts the settings list OR the frozen
    ConciergeSettings dataclass (duck-typed: anything with a
    ``managed_category_ids`` attribute) so the adapter can hand its settings
    object straight through.
    """
    if category_id is None:
        return False
    managed = getattr(managed_category_ids, "managed_category_ids",
                       managed_category_ids) or []
    return str(category_id) in {str(c) for c in managed}
