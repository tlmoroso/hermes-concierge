"""Runtime reader for the ``concierge`` config section (see hermes_cli/AGENTS.md:
every DEFAULT_CONFIG key has a reader; presence-sensitive readers use
load_user_config_effective, this reader merges DEFAULT_CONFIG because the
Discord surface wants filled-in defaults, not presence)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from hermes_cli.config_defaults import DEFAULT_CONFIG


@dataclass(frozen=True)
class ConciergeSettings:
    enabled: bool
    hub_profile: str
    managed_category_ids: list[str] = field(default_factory=list)
    default_provider_group: Optional[str] = None
    provider_groups: dict[str, dict] = field(default_factory=dict)
    location_channel_id: Optional[str] = None
    multi_user: bool = False


def concierge_settings(cfg: Optional[dict] = None) -> ConciergeSettings:
    """Merge a loaded config dict over DEFAULT_CONFIG['concierge'].

    ``cfg`` is the full config dict from any of the three loaders; None means
    "defaults only" (used by tests and by CLI flags that bypass config).
    """
    merged = dict(DEFAULT_CONFIG["concierge"])
    if cfg and isinstance(cfg.get("concierge"), dict):
        merged.update(cfg["concierge"])
    return ConciergeSettings(
        enabled=bool(merged["enabled"]),
        hub_profile=str(merged["hub_profile"]),
        managed_category_ids=[str(c) for c in merged["managed_category_ids"]],
        default_provider_group=merged["default_provider_group"],
        provider_groups=dict(merged["provider_groups"]),
        location_channel_id=(str(merged["location_channel_id"])
                             if merged.get("location_channel_id") else None),
        multi_user=bool(merged.get("multi_user", False)),
    )
