"""Runtime reader for the ``concierge`` config section (see hermes_cli/AGENTS.md:
every DEFAULT_CONFIG key has a reader; presence-sensitive readers use
load_user_config_effective, this reader merges DEFAULT_CONFIG because the
Discord surface wants filled-in defaults, not presence).

Typed surface (§8 / HOME_CATEGORY_DESIGN.md). Every new key is read through one
place so later cards never hand-roll a raw config lookup:

    settings.home_category.enabled / .visibility / .general_channel / .unbound_scope
    settings.threading.mode

The two sub-sections ship OFF (see DEFAULT_CONFIG): disabled placement,
``managed`` scope, ``mentions`` threading — byte-for-byte today's behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from hermes_cli.config_defaults import DEFAULT_CONFIG


# Nested defaults are derived from DEFAULT_CONFIG (single source of truth) so the
# dataclass field defaults can never drift from the registered config keys.
_HC_DEFAULTS = DEFAULT_CONFIG["concierge"]["home_category"]
_THREADING_DEFAULTS = DEFAULT_CONFIG["concierge"]["threading"]


@dataclass(frozen=True)
class HomeCategorySettings:
    """`concierge.home_category` — per-user space placement + orchestrator scope."""
    enabled: bool = bool(_HC_DEFAULTS["enabled"])
    visibility: str = str(_HC_DEFAULTS["visibility"])
    general_channel: str = str(_HC_DEFAULTS["general_channel"])
    unbound_scope: str = str(_HC_DEFAULTS["unbound_scope"])


@dataclass(frozen=True)
class ThreadingSettings:
    """`concierge.threading` — surface-wide auto-threading policy."""
    mode: str = str(_THREADING_DEFAULTS["mode"])


@dataclass(frozen=True)
class ConciergeSettings:
    enabled: bool
    hub_profile: str
    managed_category_ids: list[str] = field(default_factory=list)
    default_provider_group: Optional[str] = None
    provider_groups: dict[str, dict] = field(default_factory=dict)
    location_channel_id: Optional[str] = None
    multi_user: bool = False
    voice_auto_join: bool = False
    home_category: HomeCategorySettings = field(default_factory=HomeCategorySettings)
    threading: ThreadingSettings = field(default_factory=ThreadingSettings)
def _string_or_default(value: object, default: str) -> str:
    """Mirror ``_coerce_config_set_value``: string-typed settings are preserved
    verbatim so enum members such as ``threading.mode="off"`` never become
    booleans. Anything that is not a non-blank string (None, blank, wrong type)
    is junk and fails closed to the registered DEFAULT_CONFIG default.
    """
    if isinstance(value, str) and value.strip():
        return value
    return default


def _bool_or_default(value: object, default: bool) -> bool:
    """Fail-closed boolean gate. Honors YAML bools and the scalar words the
    ``hermes config set`` coercion recognizes (true/yes/on, false/no/off/null),
    but junk (a random string, list, dict, int) resolves to ``default`` — a
    garbage value must never turn a shipped-OFF feature ON.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        word = value.strip().lower()
        if word in ("true", "yes", "on"):
            return True
        if word in ("false", "no", "off", "null", "none", "~", ""):
            return False
    return default
def _merge_subsection(raw: object, default: dict) -> dict:
    """Deep-merge one nested ``concierge`` sub-section over its DEFAULT_CONFIG
    defaults. A shallow ``merged.update(cfg["concierge"])`` would otherwise
    replace the whole sub-dict, so a partial config (e.g. only ``enabled``) is
    filled in from defaults. A non-dict value is junk: defaults stay in force.
    """
    if isinstance(raw, dict):
        merged = dict(default)
        merged.update(raw)
        return merged
    return dict(default)


def concierge_settings(cfg: Optional[dict] = None) -> ConciergeSettings:
    """Merge a loaded config dict over DEFAULT_CONFIG['concierge'].

    ``cfg`` is the full config dict from any of the three loaders; None means
    "defaults only" (used by tests and by CLI flags that bypass config). The
    nested ``home_category`` / ``threading`` sub-sections are deep-merged so an
    explicit partial config keeps conservative defaults for absent keys, and
    every value is type-coerced fail-closed.
    """
    merged = dict(DEFAULT_CONFIG["concierge"])
    if cfg and isinstance(cfg.get("concierge"), dict):
        merged.update(cfg["concierge"])

    hc_default = DEFAULT_CONFIG["concierge"]["home_category"]
    threading_default = DEFAULT_CONFIG["concierge"]["threading"]
    hc = _merge_subsection(merged.get("home_category"), hc_default)
    threading = _merge_subsection(merged.get("threading"), threading_default)

    return ConciergeSettings(
        enabled=bool(merged["enabled"]),
        hub_profile=str(merged["hub_profile"]),
        managed_category_ids=[str(c) for c in merged["managed_category_ids"]],
        default_provider_group=merged["default_provider_group"],
        provider_groups=dict(merged["provider_groups"]),
        location_channel_id=(str(merged["location_channel_id"])
                             if merged.get("location_channel_id") else None),
        multi_user=bool(merged.get("multi_user", False)),
        voice_auto_join=_bool_or_default(
            merged.get("voice_auto_join"), False),
        home_category=HomeCategorySettings(
            enabled=_bool_or_default(hc.get("enabled"), hc_default["enabled"]),
            visibility=_string_or_default(hc.get("visibility"), hc_default["visibility"]),
            general_channel=_string_or_default(hc.get("general_channel"), hc_default["general_channel"]),
            unbound_scope=_string_or_default(hc.get("unbound_scope"), hc_default["unbound_scope"]),
        ),
        threading=ThreadingSettings(
            mode=_string_or_default(threading.get("mode"), threading_default["mode"]),
        ),
    )
