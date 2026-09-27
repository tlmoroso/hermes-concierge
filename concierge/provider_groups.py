"""Resolve which model provider a new bot should use, from Discord geography.

Provider groups are config data (concierge.provider_groups): a named mapping
of provider+model plus where it applies. Category match beats guild match
beats the default group — most specific wins. Credentials themselves are NOT
managed here: they live in the hub profile's .env and are copied into bot
profiles at creation (see concierge/provision.py).
"""

from __future__ import annotations

from typing import Dict, Optional

from concierge.blueprints import ModelPreset

__all__ = ["resolve_provider_group", "apply_group_to_preset"]


def _annotated(group: Dict, name: str) -> Dict:
    """The winning group as a COPY carrying its config key under ``"name"``.

    The registry binding stores that key (``bind_channel(provider_group=...)``),
    so a bot's provider lineage stays auditable after the group is resolved
    into a bare value dict — the config mapping itself is never mutated (a
    later ``resolve`` of the same groups must not see a injected key, and the
    user's config file is not this module's to change).
    """
    return {**group, "name": str(name)}


def resolve_provider_group(groups: Dict[str, Dict], *, guild_id: Optional[str],
                           category_id: Optional[str],
                           default_group: Optional[str] = None) -> Optional[Dict]:
    """Most-specific match wins: category -> guild -> default. None if nothing.

    The winner is returned ANNOTATED with its config key under ``"name"`` (see
    :func:`_annotated`) — the key the channel binding records as the bot's
    provider group. Without it every binding's ``provider_group`` was None: the
    name is the dict's key in ``concierge.provider_groups``, lost when the
    group was resolved into a bare value dict.

    Tie-break within a tier: when several groups match at the same specificity,
    the first one in config order (the insertion order of
    ``concierge.provider_groups``) wins — a later entry never shadows an
    earlier one at equal specificity, so an id served by two groups is a
    deliberate config decision, not dict-ordering luck.
    """
    for group_name, group in groups.items():
        if category_id and category_id in [str(c) for c in group.get("category_ids", [])]:
            return _annotated(group, group_name)
    for group_name, group in groups.items():
        if guild_id and guild_id in [str(g) for g in group.get("guild_ids", [])]:
            return _annotated(group, group_name)
    if default_group and default_group in groups:
        return _annotated(groups[default_group], default_group)
    return None


def apply_group_to_preset(group: Optional[Dict], preset: ModelPreset) -> ModelPreset:
    """A matched group overrides the blueprint's preset; no group = preset as-is.

    A group missing ``provider`` or ``model`` raises KeyError naming the
    missing key — a provider_groups config typo must fail here, loudly,
    not as a bare ``KeyError: 'provider'`` deep in provisioning.
    """
    if not group:
        return preset
    for key in ("provider", "model"):
        if key not in group:
            raise KeyError(
                f"provider group entry {group!r} is missing required key "
                f"'{key}'; every concierge.provider_groups entry needs both "
                "'provider' and 'model'"
            ) from None
    return ModelPreset(provider=group["provider"], model=group["model"])
