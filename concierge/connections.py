"""Concierge account connections (Task 8.2): prerequisite probing, dashboard
deep links, and the copy behind the first-message nudge + ``/connect``.

NO new OAuth plumbing (the plan's core rule): the deep links point at the
gateway dashboard's EXISTING connect surfaces. Task 8.1 discovery, pinned
here as data:

- The dashboard is the SPA the gateway serves (``mount_spa``,
  hermes_cli/web_server_dashboard.py) with CLIENT-side routes
  (apps/desktop/src/app/routes.ts). The real connect pages:
  ``/messaging`` — platform onboarding, including the email mailbox account
  (IMAP/SMTP credentials; the page's own copy: "Use a dedicated mailbox…");
  ``/settings?tab=keys`` — the ENV & credentials page, the closest existing
  surface for file-based credentials (the google-workspace skill keeps its
  OAuth token at ``<HERMES_HOME>/google_token.json``, and its setup is
  conversational — driven in chat by the skill — so there is no dedicated
  Google page to deep-link; the link lands on the credentials page and the
  copy tells the truth: ask the bot in chat).
- The dashboard's own origin is NOT guessed: ``dashboard.public_url``
  (hermes_cli/config_defaults.py) / ``HERMES_DASHBOARD_PUBLIC_URL``, read
  through the canonical resolver
  ``hermes_cli.dashboard_auth.prefix.resolve_public_url`` — the same value
  the OAuth redirect/Host-trust machinery uses. Empty (the default) means
  the server reconstructs the origin from request headers, which offline
  code cannot know: link-building then yields no links and the copy stays
  honest (link-less nudge, status-only /connect).
- The ``hermes tools`` configured-probe is ``_toolset_has_keys``
  (hermes_cli/tools_config.py) — local env/auth reads — but the concierge's
  account kinds are NOT toolsets, so ``account_probe`` checks the surfaces
  the bots actually use, per kind, with CHEAP LOCAL reads only (stat /
  ``shutil.which`` — never network; the google-workspace setup script's own
  ``--check`` does an OAuth token refresh over the network and is
  deliberately NOT used on the message path).

Purity: the decision layer (``missing_connections``, ``connect_links``,
``build_*``) is pure — plain values in, plain dicts out, no filesystem, no
discord.py, no gateway imports — so tests stub the probe. The two I/O
helpers (``account_probe``, ``dashboard_base_url``) are clearly marked and
lazy-import their dependencies.

Unknown-kind policy (picked + documented + tested): a kind that appears in
a blueprint's ``connects`` but has no entry in the path table is SKIPPED
from link building with a logged reason — never an error (a missing
mapping must not break the turn) and never a guessed link; the /connect
status line still reports it honestly as not connected, without a button.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from concierge.blueprints import CATALOG

__all__ = [
    "DASHBOARD_PATHS",
    "needed_kinds",
    "probe_profile_for",
    "missing_connections",
    "connect_links",
    "build_connect_status",
    "build_connect_kind_reply",
    "build_nudge_response",
    "account_probe",
    "dashboard_base_url",
]

logger = logging.getLogger(__name__)

# kind -> the dashboard's existing connect surface for it. DATA, not a
# ladder (a new account kind is one entry here, zero code paths). ``label``
# is the user-facing name; ``path`` is the SPA client route discovered in
# Task 8.1 (see the module docstring for what each page actually does).
DASHBOARD_PATHS: Dict[str, Dict[str, str]] = {
    "email": {"label": "Email", "path": "/messaging"},
    "calendar": {"label": "Calendar", "path": "/settings?tab=keys"},
}

# The google-workspace skill's OAuth artifact (skills/productivity/
# google-workspace/scripts/setup.py: ``TOKEN_PATH = HERMES_HOME /
# "google_token.json"``) — the bot profile's own token, since profiles are
# credential islands and a bot's turn runs scoped to its home.
_GOOGLE_TOKEN_FILE = "google_token.json"

# The himalaya CLI config the email skill reads (skills/email/himalaya/
# SKILL.md prerequisites: the CLI itself + ``$XDG_CONFIG_HOME/himalaya/
# config.toml``). User-level on the host the gateway runs on.
_HIMALAYA_COMMAND = "himalaya"
_HIMALAYA_CONFIG_RELPATH = ("himalaya", "config.toml")


# ── the pure decision layer ────────────────────────────────────────────────


def _kind_label(kind: str, kind_paths: Mapping[str, Mapping[str, str]]) -> str:
    """User-facing label for *kind*: the path table's label when mapped,
    else a plain title-case of the kind (status lines never leak slugs)."""
    entry = kind_paths.get(kind)
    if isinstance(entry, Mapping):
        label = str(entry.get("label") or "").strip()
        if label:
            return label
    return str(kind).replace("_", " ").strip().title()


def _connects_of(source: Any) -> List[str]:
    """The account kinds *source* declares: a BotBlueprint (``.connects``)
    or a bare connects iterable, duck-typed. Non-string entries are
    skipped (a hand-edited catalog must not crash the nudge decision)."""
    connects = getattr(source, "connects", source)
    if isinstance(connects, str) or not hasattr(connects, "__iter__"):
        return []
    return [k for k in connects if isinstance(k, str) and k.strip()]


def missing_connections(source: Any, probe: Callable[[str], bool]) -> List[str]:
    """The UNCONFIGURED account kinds *source* (a BotBlueprint, or a bare
    ``connects`` tuple) needs — ``connects`` order preserved, deduped.

    ``probe`` answers ``kind -> bool`` (True = configured) and is INJECTED
    so tests stub it; production wires ``account_probe(profile)``. An empty
    ``connects`` returns ``[]`` without ever calling ``probe`` — the
    reminders/researcher/notes bots pay zero hot-path probe work.
    """
    missing: List[str] = []
    for kind in _connects_of(source):
        if kind in missing:
            continue
        if not probe(kind):
            missing.append(kind)
    return missing


def connect_links(
    missing: Sequence[str],
    base_url: Optional[str],
    kind_paths: Mapping[str, Mapping[str, str]] = DASHBOARD_PATHS,
) -> List[Dict[str, str]]:
    """Deep-link payloads for the missing kinds: ``{"kind", "label", "url"}``
    per MAPPED kind, dashboard base + the discovered page path.

    - ``base_url`` blank/None → ``[]``: the dashboard's origin is unknown
      (no ``dashboard.public_url``), and an un-tappable link is worse than
      none — callers fall back to link-less copy.
    - unmapped kinds are SKIPPED with a logged reason (the documented
      unknown-kind policy: never an error, never a guess).
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return []
    links: List[Dict[str, str]] = []
    for kind in missing:
        entry = kind_paths.get(kind) if isinstance(kind, str) else None
        if not isinstance(entry, Mapping):
            logger.debug(
                "concierge connections: no dashboard path mapping for "
                "account kind %r — skipped from link building", kind)
            continue
        path = str(entry.get("path") or "")
        if not path:
            continue
        links.append({"kind": kind, "label": _kind_label(kind, kind_paths),
                      "url": f"{base}{path}"})
    return links


def needed_kinds(
    bindings: Mapping[str, Any], guild_id: Optional[str],
) -> List[str]:
    """The account kinds this guild's bound bots depend on: the deduped
    union of their blueprints' ``connects``, first-seen order.

    Guild scoping mirrors the other concierge pickers: bindings recorded for
    ANOTHER guild are that guild's business; bindings without a guild
    (CLI-provisioned) are the operator's and stay visible. Unknown blueprint
    keys and junk entries are skipped defensively — a hand-edited registry
    must not crash the /connect listing.
    """
    kinds: List[str] = []
    for binding in bindings.values():
        if not isinstance(binding, Mapping):
            continue
        if guild_id and binding.get("guild_id") not in (None, str(guild_id)):
            continue
        blueprint = CATALOG.get(str(binding.get("blueprint") or ""))
        if blueprint is None:
            continue
        for kind in _connects_of(blueprint):
            if kind not in kinds:
                kinds.append(kind)
    return kinds


def probe_profile_for(
    bindings: Mapping[str, Any], guild_id: Optional[str], kind: str,
) -> Optional[str]:
    """The profile the ``/connect`` status should probe *kind* against: the
    FIRST guild binding whose blueprint needs it, else ``None`` (no bot in
    this guild needs the kind — the caller answers without a status claim).
    Kinds are account-level (one user per server, the concierge model), so
    one representative bot answers for the guild."""
    for binding in bindings.values():
        if not isinstance(binding, Mapping):
            continue
        if guild_id and binding.get("guild_id") not in (None, str(guild_id)):
            continue
        blueprint = CATALOG.get(str(binding.get("blueprint") or ""))
        if blueprint is not None and kind in _connects_of(blueprint):
            profile = binding.get("profile")
            if isinstance(profile, str) and profile.strip():
                return profile
    return None


def build_connect_status(
    bindings: Mapping[str, Any],
    guild_id: Optional[str],
    configured: Mapping[str, bool],
    base_url: Optional[str],
    kind_paths: Mapping[str, Mapping[str, str]] = DASHBOARD_PATHS,
) -> Dict[str, Any]:
    """The ``/connect`` no-arg listing: ``{"lines", "links", "empty"}``.

    ``lines`` carry the configured/unconfigured status per needed kind
    (``configured`` is the CALLER's kind->bool, normally probed against
    ``probe_profile_for``'s profile); ``links`` are the deep links for the
    UNCONFIGURED kinds (``connect_links`` — empty when no dashboard base or
    no mapping); ``empty`` flags a guild whose bots need no accounts.
    """
    kinds = needed_kinds(bindings, guild_id)
    if not kinds:
        return {"lines": [], "links": [], "empty": True}
    lines: List[str] = []
    unconfigured: List[str] = []
    for kind in kinds:
        label = _kind_label(kind, kind_paths)
        if configured.get(kind):
            lines.append(f"✅ **{label}** — connected")
        else:
            lines.append(f"⚠️ **{label}** — not connected")
            unconfigured.append(kind)
    return {"lines": lines,
            "links": connect_links(unconfigured, base_url, kind_paths),
            "empty": False}


def build_connect_kind_reply(
    kind: str,
    configured: Optional[bool],
    base_url: Optional[str],
    kind_paths: Mapping[str, Mapping[str, str]] = DASHBOARD_PATHS,
) -> Dict[str, Any]:
    """The ``/connect <kind>`` reply: ``{"content", "link"}`` where ``link``
    is a ``connect_links`` entry (or ``None`` when there is nothing to tap).

    ``configured=None`` means no bot in this guild needs the kind — the
    user asked explicitly, so the link is offered without a status claim.
    Unmapped kinds get an actionable refusal naming what CAN be linked.
    """
    kind = str(kind or "").strip()
    entry = kind_paths.get(kind)
    if not isinstance(entry, Mapping):
        linkable = ", ".join(f"`{k}`" for k in kind_paths)
        return {"content": (
            f"`{kind}` isn't an account I can link yet — try one of: "
            f"{linkable}."), "link": None}
    label = _kind_label(kind, kind_paths)
    if configured is True:
        return {"content": f"✅ Your **{label}** is already connected — "
                           f"nothing to do.", "link": None}
    links = connect_links([kind], base_url, kind_paths)
    if links:
        return {"content": f"Connect your **{label}** so your bots can use "
                           f"it:", "link": links[0]}
    if configured is False:
        return {"content": f"Your **{label}** isn't connected yet — ask the "
                           f"server owner to finish setting it up.",
                "link": None}
    return {"content": f"Connect your **{label}** — ask the server owner if "
                       f"you need a hand.", "link": None}


def build_nudge_response(
    missing: Sequence[str],
    links: Sequence[Mapping[str, str]],
    kind_paths: Mapping[str, Mapping[str, str]] = DASHBOARD_PATHS,
) -> Dict[str, str]:
    """The first-message nudge copy: ``{"text"}``.

    With links (Task 8.2's shape, adapted naturally): "I need access to your
    **Calendar** to do this — tap here to connect: [Connect Calendar](url)";
    multiple kinds become a bullet list. Without links (no dashboard base
    URL, or an unmapped kind) the nudge still fires — copy names the kind
    and points at ``/connect`` — because the missing prerequisite is the
    user's problem to know about; a dead URL is worse than no URL.
    """
    by_kind = {str(link.get("kind")): link for link in links
               if isinstance(link, Mapping)}
    rows: List[str] = []
    for kind in missing:
        label = _kind_label(str(kind), kind_paths)
        link = by_kind.get(str(kind))
        if link and link.get("url"):
            rows.append(f"• **{label}** — [Connect]({link['url']})")
        else:
            rows.append(f"• **{label}** — no link yet; run `/connect` for "
                        f"details")
    if not rows:
        return {"text": ""}
    if len(rows) == 1:
        kind = str(missing[0])
        link = by_kind.get(kind)
        if link and link.get("url"):
            label = _kind_label(kind, kind_paths)
            return {"text": (
                f"I need access to your **{label}** to do this — "
                f"tap here to connect: [Connect {label}]({link['url']})")}
        return {"text": (
            f"I need access to your **{_kind_label(kind, kind_paths)}** "
            f"to do this — run `/connect` to see how to connect it.")}
    return {"text": "I need access to a few of your accounts to do this:\n"
                    + "\n".join(rows)}


# ── the I/O helpers (cheap local reads; lazy imports) ──────────────────────


def _calendar_configured(profile: str) -> bool:
    """The bot profile's google-workspace OAuth token exists — a local
    stat. The skill's own ``setup.py --check`` would refresh the token over
    the network (deliberately not used on the message path); limitation:
    a present-but-expired token reads as connected — the bot's setup flow
    re-auths and the nudge has already done its job."""
    from hermes_cli.profiles import get_profile_dir

    return bool((get_profile_dir(profile) / _GOOGLE_TOKEN_FILE).is_file())


def _email_configured(_profile: str) -> bool:
    """The email skill's prerequisites are usable: the himalaya CLI on PATH
    and its config file (``$XDG_CONFIG_HOME/himalaya/config.toml``, else
    ``~/.config/himalaya/config.toml``). Local reads only — a PATH scan and
    one stat; the config is host-level, so the profile is unused."""
    if shutil.which(_HIMALAYA_COMMAND) is None:
        return False
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    config_root = Path(xdg) if xdg else Path.home() / ".config"
    return (config_root.joinpath(*_HIMALAYA_CONFIG_RELPATH)).is_file()


# kind -> checker(profile) -> configured?  DATA: a new account kind is one
# entry here plus one in DASHBOARD_PATHS. Kinds without a checker read as
# CONFIGURED (fail-open — the nudge must be certain before it nags).
_KIND_CHECKS: Dict[str, Callable[[str], bool]] = {
    "calendar": _calendar_configured,
    "email": _email_configured,
}


def account_probe(profile: str) -> Callable[[str], bool]:
    """The REAL probe factory: ``account_probe(profile)(kind) -> bool``.

    CHEAP-LOCAL by contract — stat / PATH-scan / env reads, never network —
    because it runs on the message path (bounded: once per binding, only
    for blueprints with un-nudged connects). Unknown kinds and ANY checker
    failure read as CONFIGURED: the nudge must be certain before it nags
    the user (a broken probe must not manufacture a false nudge).
    """
    def _probe(kind: str) -> bool:
        check = _KIND_CHECKS.get(kind)
        if check is None:
            logger.debug(
                "concierge connections: no configured-check for account "
                "kind %r; treating as configured (fail-open)", kind)
            return True
        try:
            return bool(check(profile))
        except Exception:
            logger.debug(
                "concierge connections: configured-check for %r raised; "
                "treating as configured (fail-open)", kind, exc_info=True)
            return True

    return _probe


def dashboard_base_url() -> str:
    """The dashboard's own public origin — ``resolve_public_url``
    (hermes_cli.dashboard_auth.prefix; env ``HERMES_DASHBOARD_PUBLIC_URL``
    → config ``dashboard.public_url``), the same value the gateway's OAuth
    redirect/Host-trust machinery uses. ``""`` when unset/unknown. Never
    raises: link-building degrades to link-less copy, it never breaks a
    turn or a command."""
    try:
        from hermes_cli.dashboard_auth.prefix import resolve_public_url

        return str(resolve_public_url() or "")
    except Exception:
        logger.debug("concierge connections: dashboard public URL could "
                     "not be resolved; deep links disabled",
                     exc_info=True)
        return ""
