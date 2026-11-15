"""Pure, Discord-agnostic HOME-SPACE provisioning (HOME_CATEGORY_DESIGN §3.1/§3.2).

The operator CLI (``hermes concierge new orchestrator --user X --category Y``)
and the Discord admin surface (``/bot category`` / ``/bot space``, card H) both
need the same NON-DISCORD tail of home-space setup:

    * ensure-orchestrator — provision the user's orchestrator ONLY when they
      have none (idempotent; a re-run adopts the existing profile);
    * record the ``home_category`` + ``home_channel`` + ``guild_id`` triple;
    * ``bind_channel`` LAST — nothing routes into the general channel until the
      binding lands.

The DISCORD part (creating the category / general channel, greeting, resolving
ids through a live gateway client) stays in each lane: card F's mixin helpers
for the admin surface, ``_create_home_space_channel`` for the offline CLI. This
module owns NO Discord dependency and NO profile-creation logic — it reuses
``concierge.provision.create_bot`` and the ``concierge.registry`` writers, so
the ordering/registry contract is expressed exactly ONCE for both lanes
(operator decision 2026-11-14).

``home_general_channel_name`` is the SINGLE pure function that decides a HOME
space's general-channel name (card-A config-driven, default ``general``). Both
the CLI and the mixin's one naming authority call it; the no-home-category
fallback name (``orchestrator-<slug>``) stays in the mixin (design §3.1).
"""

from __future__ import annotations

from typing import Optional

__all__ = ["HomeSpaceError", "home_general_channel_name", "provision_home_space"]


class HomeSpaceError(RuntimeError):
    """A home-space step failed. ``step`` is one of provision/record/bind so a
    caller can phrase an operator-facing message; ``cause`` is the underlying
    exception (never swallowed)."""

    def __init__(self, step: str, cause: BaseException):
        self.step = step
        self.cause = cause
        super().__init__(f"{step}: {cause}")


def home_general_channel_name(settings=None) -> str:
    """THE single pure function deciding a HOME space's general-channel name.

    Inside a home category the name is card-A's config-driven
    ``concierge.home_category.general_channel`` (registered default
    ``general``); the no-home-category fallback (``orchestrator-<slug>``,
    bootstrap / legacy placement) lives in the mixin's naming authority and
    deliberately does NOT consult this. A missing/unreadable settings object
    fails closed to ``general`` — the name is cosmetic, never fatal.

    ``settings`` may be an already-loaded typed settings object (the CLI's
    ``concierge_settings(load_config())``); ``None`` loads defaults through
    ``concierge_settings()`` (the mixin lane and tests).
    """
    try:
        if settings is None:
            from concierge.settings import concierge_settings
            settings = concierge_settings()
        value = settings.home_category.general_channel
        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:  # health: allow BLE001 -- cosmetic name; any reader fault fails closed to 'general'
        return "general"
    return "general"


def provision_home_space(
    *,
    user_id,
    guild_id,
    home_channel: str,
    home_category: str,
    blueprint: str = "orchestrator",
    provider_group: Optional[dict] = None,
    display_name: Optional[str] = None,
) -> dict:
    """The shared, non-Discord tail of home-space provisioning (sync, pure).

    Called by BOTH lanes: the offline operator CLI
    (``hermes_cli/concierge.py::_cmd_new_home_space``) and the Discord admin
    surface (``concierge_mixin.py::_concierge_build_home_space`` via
    ``asyncio.to_thread``). Reuses ``provision.create_bot`` + the registry
    writers; owns NO provisioning logic and NO Discord client.

    Order (HOME_CATEGORY_DESIGN §3.1): ensure-orchestrator -> record triple ->
    ``bind_channel`` LAST. Any step fault raises ``HomeSpaceError`` tagged with
    the step (the caller phrases the operator-facing message); nothing is
    swallowed. Returns ``{"profile", "already_provisioned"}`` so the CLI can
    print its honest header and both lanes can report the orchestrator.
    """
    from concierge import provision, registry

    bot_kwargs = {"user_id": user_id, "guild_id": guild_id,
                  "channel_id": home_channel}
    if provider_group is not None:
        bot_kwargs["provider_group"] = provider_group
    if display_name is not None:
        bot_kwargs["display_name"] = display_name

    step = "provision"
    try:
        # 1) ensure-orchestrator: provision ONLY when the user has none.
        profile = registry.orchestrator_for_user(user_id)
        already = profile is not None
        if not profile:
            record = provision.create_bot(blueprint, **bot_kwargs)
            profile = (record.get("profile") or record.get("orchestrator")
                       if isinstance(record, dict) else None)
            if not profile:
                raise RuntimeError("no orchestrator profile returned")
            already = bool(record.get("already_provisioned"))

        # 2) record home_category + home_channel + guild_id (authoritative).
        step = "record"
        registry.update_user(
            user_id, home_category=home_category, home_channel=home_channel,
            guild_id=guild_id)

        # 3) bind_channel LAST — nothing routes into the channel until it lands.
        step = "bind"
        bind_kwargs = {"profile": profile, "blueprint": blueprint,
                       "guild_id": guild_id}
        if isinstance(provider_group, dict):
            name = provider_group.get("name")
            if name is not None:
                bind_kwargs["provider_group"] = name
        registry.bind_channel(home_channel, **bind_kwargs)

        return {"profile": profile, "already_provisioned": already}
    except Exception as exc:  # health: allow BLE001 -- a home-space service/registry fault becomes a step-tagged HomeSpaceError, never swallowed
        raise HomeSpaceError(step, exc) from exc
