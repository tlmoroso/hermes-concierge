"""Location events (Task 10.1): route a phone's region enter/exit to the bound reminder bot.

An iOS Shortcuts automation ("When I arrive at <location>") — or the Mac findmy
fallback documented in DISCORD_INTEGRATION.md — POSTs a small JSON payload to
the gateway's ``concierge-location`` webhook route:

    {"user": "tim", "region": "work", "event": "enter"}   # or "exit"

The route kind (``handler: concierge-location`` in gateway/platforms/webhook.py)
resolves the run target here: the bot bound to the channel named by
``concierge.location_channel_id``, plus the prompt for the event. Everything is
pure data-in/data-out over the registry and config reads — unit-testable with a
temp home; a garbage payload can never raise.

The run itself is the webhook's own agent-run spawn: the turn is scoped to the
bound bot's profile (``source.profile``) and its reply is delivered to the bound
Discord channel through the hub's adapters — the same origin delivery
(``{'platform': 'discord', 'chat_id': <channel>}``) the bot's cron routine uses.

Auth on the route is the webhook's existing per-route token: an HMAC secret the
Shortcut sends as the ``X-Gitlab-Token`` header (the plain shared-token mode the
signature validator already supports — iOS Shortcuts cannot compute HMACs).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from concierge import registry
from concierge.settings import concierge_settings

logger = logging.getLogger(__name__)

__all__ = ["handle_location_event", "resolve_location_run"]

#: The only event values a region automation can send.
_VALID_EVENTS = ("enter", "exit")

#: A region name longer than this is garbage, not a place the user named.
_MAX_REGION_LEN = 100

#: Debug-log cap for payload reprs: the webhook body limit is 1MB, and an
#: invalid payload's reason log must not mirror attacker-sized input wholesale.
_PAYLOAD_LOG_LIMIT = 200


def _payload_repr(payload: Any, limit: int = _PAYLOAD_LOG_LIMIT) -> str:
    """Truncated ``repr(payload)`` for debug logs (first ``limit`` chars +
    '...'). ``user`` itself is never length-capped — it is unused downstream."""
    text = repr(payload)
    return text if len(text) <= limit else text[:limit] + "..."

# The prompt per event kind. {region} is the payload's region name; the copy is
# deliberately short and phone-first: the bot posts at most a brief agenda.
_LOCATION_PROMPTS = {
    "enter": "The user just arrived at {region}. Deliver any {region}-related "
             "reminders now, briefly.",
    "exit": "The user just left {region}. Deliver any reminders tied to leaving "
            "{region} now, briefly.",
}


def _load_cfg(cfg: Optional[dict]) -> Optional[dict]:
    """``cfg`` as given, or the config.yaml from the active home (defaults-only
    when unreadable) — so the plan's bare ``handle_location_event(payload)``
    signature works standalone."""
    if cfg is not None:
        return cfg
    try:
        from hermes_cli.config import load_config
        return load_config()
    except Exception:  # unreadable/absent config: defaults-only, never a raise
        logger.debug("concierge location: config load failed; defaults only",
                     exc_info=True)
        return None


def _validated_location(payload: Any) -> Optional[tuple]:
    """(user, region, event) from the payload, or None with a logged reason.

    The payload comes from an HTTP POST; nothing here may raise.
    """
    if not isinstance(payload, dict):
        logger.debug("concierge location: payload is not a JSON object (%r)",
                     type(payload).__name__)
        return None
    user = payload.get("user")
    region = payload.get("region")
    event = payload.get("event")
    if not isinstance(user, str) or not user.strip():
        if logger.isEnabledFor(logging.DEBUG):  # cap + laziness: no repr of a 1MB body unless logging
            logger.debug("concierge location: missing or invalid 'user' in %s",
                         _payload_repr(payload))
        return None
    if not isinstance(region, str) or not region.strip():
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("concierge location: missing or invalid 'region' in %s",
                         _payload_repr(payload))
        return None
    region = region.strip()
    if len(region) > _MAX_REGION_LEN:
        logger.debug("concierge location: region name over %d chars — garbage",
                     _MAX_REGION_LEN)
        return None
    if event not in _VALID_EVENTS:
        logger.debug("concierge location: unknown 'event' %r (want one of %s)",
                     event, "/".join(_VALID_EVENTS))
        return None
    return user.strip(), region, event


def resolve_location_run(payload: Any, cfg: Optional[dict] = None) -> Optional[dict]:
    """Resolve the location-event run target, or None when there is none.

    Returns ``{"prompt", "profile", "channel_id", "region", "event", "user"}``
    for the webhook route kind to fire — the prompt to run, the bound bot's
    profile (the run scope) and the bound channel (the delivery target).
    None (with a logged reason) when the payload is garbage, the channel is
    unset, nothing is bound, or the bound bot's profile is gone.

    ``cfg`` is the full config dict from any loader; None loads config.yaml
    from the active home (defaults-only when unreadable).
    """
    validated = _validated_location(payload)
    if validated is None:
        return None
    user, region, event = validated

    settings = concierge_settings(_load_cfg(cfg))
    channel_id = settings.location_channel_id
    if not channel_id:
        logger.debug("concierge location: concierge.location_channel_id is not set "
                     "— no reminder channel to route to")
        return None

    try:
        binding = registry.get_binding(channel_id)
    except OSError:
        # Registry exists but is unreadable (EACCES/EIO/stalled mount):
        # load_registry already WARNING'd once per path (M5b discipline), and
        # the unreadable-registry posture is the hub/ignored fallback — never
        # a raise out of this pure API (an aiohttp 500 here would break the
        # webhook's 200-ignored convention). Debug, not warning: the loud
        # once-per-path traceback belongs to the registry read itself.
        logger.debug("concierge location: registry unreadable — ignoring event",
                     exc_info=True)
        return None
    if not binding:
        logger.debug("concierge location: no bot bound to channel %s "
                     "(concierge.location_channel_id)", channel_id)
        return None
    profile = binding.get("profile")
    if not profile:
        logger.debug("concierge location: channel %s binding has no profile",
                     channel_id)
        return None

    # A binding whose profile dir is gone (bot retired under us, hand edit)
    # never runs — the turn would fire into a scope that does not exist.
    # Same fail-closed disposition as the Discord routing seam.
    try:
        from hermes_cli.profiles import profile_exists
        if not profile_exists(str(profile)):
            logger.debug("concierge location: bound profile %r does not exist "
                         "(retired?) — ignoring event", profile)
            return None
    except Exception:  # the check itself failing is not a reason to run
        logger.debug("concierge location: profile check failed for %r", profile,
                     exc_info=True)
        return None

    return {
        "prompt": _LOCATION_PROMPTS[event].format(region=region),
        "profile": str(profile),
        "channel_id": str(channel_id),
        "region": region,
        "event": event,
        "user": user,
    }


def handle_location_event(payload: Any, cfg: Optional[dict] = None) -> Optional[str]:
    """The plan's Task 10.1 API: the prompt to run for a location event, or None.

    Pure data-in/data-out plus registry/config reads — never raises.
    """
    run = resolve_location_run(payload, cfg)
    return run["prompt"] if run else None
