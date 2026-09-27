"""Pure Discord payload builders for the Concierge surface (Tasks 5.4/5.5/5.7).

Everything here is a pure function: plain-value inputs in, plain dicts out —
NO filesystem, NO discord.py imports, NO gateway imports. The mixin calls these
to build what it then renders through real discord.py objects; the tests pin
the DECISIONS (which options, which rows, create-vs-bind) without the event
plumbing, which the Task 5.6 manual E2E verifies live.

Rationale (plan Task 5.4): "the select-menu payload construction is pure —
factor blueprint_choices() usage and the group resolution into a testable
helper in concierge/discord_payloads.py".

Shapes (what the mixin expects back):

- ``build_new_bot_response()`` → ``{"choices": [...], "content": str}``;
  ``choices`` are ``blueprint_choices()`` dicts in CATALOG order (the picker
  data), ``content`` the greeting copy the picker message carries.
- ``build_list_payload(bindings, status_fn)`` → ``{"lines": [str, ...],
  "empty": bool}`` — one line per bot, title + channel ids + routine state.
- ``build_retire_choices(bindings, guild_id)`` → the invoking guild's bound
  bots as picker choices (profile name + title), so ``/bot retire`` offers
  only what this guild can see.
- ``should_auto_offer(category_id, settings)`` (lives in routing.py — the
  plan places the DECISION there, next to resolve_target_profile; re-exported
  here only in docs, not code).
- ``create_vs_bind(...)`` → a ``ChannelPlan`` value object: does the hub
  create a fresh channel for the new bot (Task 5.7), or bind the invoking
  channel; and when creation is the plan, what to name it.

Anti-injection: channel names and blueprint titles flow into Discord strings,
never into file paths or code — the hub's channel-create call receives the
name only as data (see the mixin's ``_concierge_create_channel``).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from concierge.blueprints import CATALOG, get_blueprint
from concierge.registry import MAX_ROOM_MEMBERS

__all__ = [
    "ChannelPlan",
    "build_new_bot_response",
    "build_list_payload",
    "build_retire_choices",
    "build_room_member_choices",
    "validate_room_selection",
    "room_channel_name",
    "build_room_created_response",
    "build_room_disband_response",
    "build_room_info_response",
    "create_vs_bind",
    "channel_name_for_blueprint",
]

# Discord's channel-name rules: lowercase, hyphens instead of spaces, and a
# short length cap. We apply our own normalization on top so a title with
# punctuation ("Email Triage Bot") still lands on a clean slug.
_CHANNEL_NAME_MAX_LEN = 90
_CHANNEL_NAME_PREFIX = "bot"


class ChannelPlan:
    """The Task 5.7 decision: create a channel or bind the invoking one.

    ``mode`` is ``"create"`` or ``"bind"``; ``channel_name`` is set only in
    create mode (the ``#<bot-name>`` the hub will create); ``reason`` carries
    the plan's provenance for logging (why bind won when it did).
    A value object, not a promise: always truthy, so test ``plan.mode``.
    """

    __slots__ = ("mode", "channel_name", "reason")

    def __init__(self, mode: str, channel_name: Optional[str] = None,
                 reason: str = "") -> None:
        self.mode = mode
        self.channel_name = channel_name
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"ChannelPlan(mode={self.mode!r}, channel_name={self.channel_name!r}, "
                f"reason={self.reason!r})")


def build_new_bot_response() -> Dict[str, Any]:
    """The blueprint picker payload for ``/bot new`` (and Task 5.5 auto-offer).

    ``choices`` is the catalog rendered as select-menu options (title/description
    — users never see Hermes concepts); ``content`` is the message copy. The
    picker is guild-independent: the same five bots everywhere, provider
    groups being a provisioning-time concern, not a picker-time one.
    """
    from concierge.blueprints import blueprint_choices

    content = (
        "✨ **Set up a bot in this channel** — pick one below and it'll be live "
        "in seconds."
    )
    return {"choices": blueprint_choices(), "content": content}


def build_list_payload(
    bindings: Mapping[str, Mapping[str, Any]],
    status_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
    *,
    guild_id: Optional[str] = None,
) -> Dict[str, Any]:
    """``/bot list`` rows: one line per bound bot (grouped by profile).

    ``status_fn(profile_name) -> bot_status()``-shaped dict supplies routine
    state (name + paused); ``None`` means "no routine info" (the line omits
    it). Guild scoping: when ``guild_id`` is given, only bindings recorded for
    that guild are listed — a bot bound in another server is another server's
    business. Bindings recorded without a guild (CLI-provisioned) are always
    shown (they are the operator's, not a guild's).
    """
    rows: List[Tuple[str, List[str]]] = []
    for channel_id, binding in bindings.items():
        if not isinstance(binding, Mapping):
            continue
        if guild_id and binding.get("guild_id") not in (None, str(guild_id)):
            continue
        profile = binding.get("profile")
        if not isinstance(profile, str) or not profile:
            continue
        if rows and rows[-1][0] == profile:
            rows[-1][1].append(str(channel_id))
        else:
            for i, (p, _chans) in enumerate(rows):
                if p == profile:
                    rows[i][1].append(str(channel_id))
                    break
            else:
                rows.append((profile, [str(channel_id)]))

    if not rows:
        return {"lines": [], "empty": True}

    lines: List[str] = []
    for profile, channels in rows:
        try:
            title = get_blueprint(str(_blueprint_of(bindings, profile))).title
        except KeyError:
            title = profile
        line = f"◆ **{title}** (`{profile}`) — {', '.join(channels)}"
        if status_fn is not None:
            try:
                status = status_fn(profile) or {}
            except Exception:
                status = {}
            routines = status.get("routines") or []
            if routines:
                states = ", ".join(
                    f"{r.get('name', 'routine')}"
                    f" ({'paused' if r.get('paused') else 'scheduled'})"
                    for r in routines
                )
                line += f"\n    ↳ {states}"
        lines.append(line)
    return {"lines": lines, "empty": False}


def _blueprint_of(bindings: Mapping[str, Mapping[str, Any]], profile: str) -> str:
    """First binding's blueprint key for a profile (title lookup only)."""
    for binding in bindings.values():
        if isinstance(binding, Mapping) and binding.get("profile") == profile:
            bp = binding.get("blueprint")
            if isinstance(bp, str) and bp:
                return bp
    return profile


def _guild_bound_bots(
    bindings: Mapping[str, Mapping[str, Any]], guild_id: Optional[str],
) -> List[Tuple[str, str, List[str]]]:
    """This guild's bound bots as ``(profile, title, channel_ids)`` rows —
    the shared guild-scoping core behind /bot retire's and /room's pickers.

    A bot bound in another guild is another guild's business; bindings
    recorded without a guild (CLI-provisioned) are the operator's and stay
    eligible everywhere. One row per PROFILE (a bot bound in several of this
    guild's channels is still one persona), channels collected for the
    description."""
    seen: Dict[str, Tuple[str, str, List[str]]] = {}
    for channel_id, binding in bindings.items():
        if not isinstance(binding, Mapping):
            continue
        if guild_id and binding.get("guild_id") not in (None, str(guild_id)):
            continue
        profile = binding.get("profile")
        if not isinstance(profile, str) or not profile:
            continue
        row = seen.get(profile)
        if row is None:
            try:
                title = get_blueprint(str(binding.get("blueprint"))).title
            except KeyError:
                title = profile
            seen[profile] = (profile, title, [str(channel_id)])
        else:
            row[2].append(str(channel_id))
    return list(seen.values())


def build_retire_choices(
    bindings: Mapping[str, Mapping[str, Any]], guild_id: Optional[str],
) -> List[Dict[str, str]]:
    """Picker choices for ``/bot retire``: this guild's bots only.

    A bot bound in another guild is not this guild's to retire (the retire
    flow pauses routines and unbinds channels everywhere — the picker just
    must not OFFER what the invoking user can't see). Choices are
    ``{value: profile, label: title, description: channels}`` in profile
    order for stability.
    """
    return [
        {"value": profile, "label": title, "description": f"Channels: {', '.join(channels)}"}
        for profile, title, channels in sorted(
            _guild_bound_bots(bindings, guild_id), key=lambda row: row[0])
    ]


def channel_name_for_blueprint(blueprint_key: str) -> str:
    """``#bot-reminder``-style channel name for a Task 5.7 created channel.

    Lowercase, hyphens for spaces, punctuation dropped, the " Bot" title
    suffix dropped (the ``bot-`` prefix already says it), capped. A duplicate
    name is Discord's problem (it refuses or suffixes); the hub never
    guesses — the create call either returns the channel or the plan falls
    back to binding the invoking channel.
    """
    try:
        title = get_blueprint(blueprint_key).title
    except KeyError:
        title = blueprint_key
    title = title.removesuffix(" Bot") if isinstance(title, str) else title
    slug = "".join(c if c.isalnum() else "-" for c in title.lower())
    slug = "-".join(part for part in slug.split("-") if part)
    name = f"{_CHANNEL_NAME_PREFIX}-{slug}"[:_CHANNEL_NAME_MAX_LEN]
    return name


def create_vs_bind(
    *,
    can_create_channels: bool,
    category_id: Optional[str],
    managed_category_ids: "Any",
    channel_id: Optional[str],
    prefer_create: bool = False,
) -> ChannelPlan:
    """Task 5.7 decision: does the hub create the bot's channel, or bind the
    invoking one?

    - ``create`` when the caller is the ``/bot new`` flow (``prefer_create``),
      the hub may create channels, and the invoking channel sits in a MANAGED
      category — the user is in bot country asking for another bot, and the
      concierge model is one bot per channel, so the new bot gets a fresh
      ``#<bot-name>`` there ("one message from the user, channel appears, bot
      says hi").
    - ``bind`` otherwise — including the Task 5.5 auto-offer pickers, whose
      sites thread ``prefer_create=False`` (the channel the picker was
      posted in was just created BY the user; provisioning there is the
      whole point) — and the plan's REQUIRED fallback: "if creation is
      denied (permissions), fall back to binding the invoking channel —
      never fail the provisioning over it."

    ``managed_category_ids`` accepts the settings list OR the frozen
    ConciergeSettings dataclass (duck-typed: anything with
    ``managed_category_ids``); values are string-compared so int-shaped ids
    from YAML configs match.
    """
    managed = getattr(managed_category_ids, "managed_category_ids",
                      managed_category_ids) or []
    managed_strs = {str(c) for c in managed}
    in_managed = category_id is not None and str(category_id) in managed_strs

    if prefer_create and can_create_channels and in_managed:
        return ChannelPlan("create", reason="hub creates the channel in the managed category")

    if channel_id is not None:
        if in_managed and not can_create_channels:
            return ChannelPlan(
                "bind", reason="channel creation denied — binding the invoking channel")
        return ChannelPlan("bind", reason="binding the invoking channel")

    # No invoking channel and creation impossible/denied: nothing to bind.
    return ChannelPlan("bind", channel_name=None,
                       reason="no invoking channel and channel creation unavailable")


# ── Task 7.4: /room payload builders ───────────────────────────────────────


def build_room_member_choices(
    bindings: Mapping[str, Mapping[str, Any]], guild_id: Optional[str],
) -> List[Dict[str, str]]:
    """Multi-select choices for ``/room create``: this guild's BOUND bots
    (plan: 2–6 of this guild's bound bots — a member must be a bot this
    guild already sees, so an unbound or other-guild name can never be
    picked). Same guild scoping and per-profile dedupe as the retire
    picker (``_guild_bound_bots``); insertion order (binding order) keeps
    the picker stable across syncs."""
    return [
        {"value": profile, "label": title, "description": f"Channels: {', '.join(channels)}"}
        for profile, title, channels in _guild_bound_bots(bindings, guild_id)
    ]


def validate_room_selection(
    values: Sequence[Any], choices: Sequence[Mapping[str, Any]],
) -> Optional[str]:
    """Validate a ``/room create`` multi-select: ``None`` when the selection
    is a valid room membership, user-language error copy otherwise.

    Rules (each documented + tested): every value must be one of this
    guild's bound bots (the choices) — an unbound or other-guild name is
    refused, never silently dropped; 2–6 members (MAX_ROOM_MEMBERS — the
    same cap the registry enforces on write; the UX layer and the registry
    are the two halves of one bound); duplicates collapse (a member runs
    once — resolve_room_targets collapses again, so this is copy clarity,
    not correctness)."""
    allowed = {str(c.get("value")) for c in choices if isinstance(c, Mapping)}
    picked: List[str] = []
    for value in values or ():
        name = str(value)
        if name not in allowed:
            return (f"`{name}` isn't one of this server's bots — "
                    f"check `/bot list`.")
        if name not in picked:
            picked.append(name)
    if len(picked) < 2:
        return "A room needs at least two bots — pick 2–6."
    if len(picked) > MAX_ROOM_MEMBERS:
        return (f"A room holds at most {MAX_ROOM_MEMBERS} bots — "
                f"pick fewer.")
    return None


def room_channel_name(topic: Optional[str]) -> str:
    """``#room-<topic>`` for a Task 7.4 created room channel (the Task 5.7
    ``channel_name_for_blueprint`` sibling for rooms): lowercase slug,
    hyphens for spaces/punctuation, capped; a blank/absent topic is a plain
    ``room`` (Discord tolerates duplicate names — it never guesses, and
    neither do we)."""
    text = (topic or "").strip()
    if not text:
        return "room"
    slug = "".join(c if c.isalnum() else "-" for c in text.lower())
    slug = "-".join(part for part in slug.split("-") if part)
    return f"room-{slug}"[:_CHANNEL_NAME_MAX_LEN] if slug else "room"


def build_room_created_response(member_titles: Sequence[str]) -> str:
    """The greeting posted INTO a freshly created room channel (public —
    everyone in the channel sees who lives here), naming every member."""
    names = ", ".join(f"**{t}**" for t in member_titles)
    return (f"✨ This is a bot room — {names} all answer here. "
            f"Say hi (mention them together or one at a time).")


def build_room_disband_response(member_titles: Sequence[str]) -> str:
    """``/room disband`` confirmation copy: names the members, and states
    the contract — the CHANNEL stays, the bots stop answering in it."""
    names = ", ".join(f"**{t}**" for t in member_titles)
    return (f"Disband this room? {names} will stop answering here — "
            f"the channel itself stays.")


def build_room_info_response(member_titles: Sequence[str]) -> str:
    """Current-membership line for ``/room add``/``/room remove`` replies:
    who is in the room now, against the cap."""
    names = ", ".join(f"**{t}**" for t in member_titles)
    return f"Room ({len(member_titles)}/{MAX_ROOM_MEMBERS}): {names}"
