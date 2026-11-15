"""Reconciliation of a user's durable ``home_category`` pointer (design §6).

Pure, Discord-agnostic. The one question this module answers is: *does this
user's home category still resolve?*  A category id can be recorded in the
registry and then invalidated out from under us by a manual Discord delete, or
the liveness lookup itself can fail (EACCES / EIO / a stalled mount). Both must
degrade quietly to today's behavior: no wedged routing, no silent rebuild of an
admin-created space, no second orchestrator.

Reuses the bootstrap's EXISTING posture vocabulary (``concierge_mixin.py``
``_concierge_user_orchestrator``, design §6 rule 5) — ``live`` / ``ghost`` /
``stat_error`` / ``first_contact`` — and nothing else. There is deliberately no
second taxonomy: callers switch on the posture string.

    first_contact  no usable pointer recorded      -> today's behavior, no warn
    live           resolve(cid) -> a real object   -> use the id
    ghost          resolve(cid) -> None            -> degrade + warn once
    stat_error     resolve(cid) raised             -> degrade + warn once

``resolve`` is injected (a ``client.get_channel``-shaped callable) so this is
unit-testable without discord.py and so the ONE-READ hot-path budget stays with
the caller. Nothing here creates a category, provisions a bot, or raises.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

__all__ = [
    "DEGRADED_POSTURES",
    "POSTURES",
    "UNCHECKABLE",
    "classify_home_category",
    "is_degraded",
    "read_home_category",
    "resolve_to_id",
]

# The bootstrap's four postures — the ONLY vocabulary (design §6 rule 5).
POSTURES = ("live", "ghost", "stat_error", "first_contact")

# The two postures that must degrade: the pointer is recorded but Discord can no
# longer resolve it (ghost), or the liveness lookup itself failed (stat_error).
# Both fall through to today's behavior and warn ONCE per user; neither may
# auto-re-create the category or provision over the fault.
DEGRADED_POSTURES = frozenset(("ghost", "stat_error"))


class _Uncheckable:
    """Sentinel: no liveness oracle is available on this install/message.

    When the Discord client offers no ``get_channel`` (e.g. a voice-only
    ingress, a test rig, startup before cache sync) we CANNOT confirm the
    category is gone. Fail-closed toward ADOPTING the recorded pointer (treat as
    ``live``): never invent an incident we cannot confirm, and never create a
    duplicate over an absence — the same posture as ``/bot space``'s
    ``_concierge_home_space_category_resolves`` helper.
    """

    __slots__ = ()


UNCHECKABLE = _Uncheckable()


def read_home_category(value: Any) -> Optional[str]:
    """Tolerant read of a stored ``home_category``: a non-blank string, else
    ``None``. Parallel to ``registry._normalize_user_entry`` so a hand-edited
    file never crashes a reader. Junk (int, None, blank, wrong type) is not an
    incident — it is simply ``first_contact`` / "no pointer recorded".
    """
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def classify_home_category(
    home_category: Any,
    resolve: Optional[Callable[[str], Any]],
) -> tuple[Optional[str], str]:
    """Return ``(category_id, posture)`` for a recorded home-category pointer.

    - ``(None, "first_contact")`` — no usable pointer: nothing to reconcile,
      today's behavior, ``resolve`` is NOT called.
    - ``(cid, "live")`` — the pointer resolves (or cannot be checked, see
      ``UNCHECKABLE``): safe to use for placement / in-scope widening.
    - ``(cid, "ghost")`` — ``resolve`` returned ``None``: Discord invalidated
      the category. Degrade + warn once, never re-create.
    - ``(cid, "stat_error")`` — ``resolve`` RAISED (OSError EACCES/EIO,
      ClientException, a stalled mount, ...): degrade identically, never
      provision over the fault.

    ``resolve`` receives the string category id and returns the Discord
    channel/category object, ``None``, or ``UNCHECKABLE`` — or raises. The
    caller is responsible for any int conversion; this function never creates
    anything, never does registry I/O, and NEVER raises (any unclassifiable
    failure resolves to ``stat_error`` — fail-closed).
    """
    cid = read_home_category(home_category)
    if cid is None:
        return None, "first_contact"
    if not callable(resolve):
        # No liveness oracle: cannot confirm a delete -> adopt, never degrade.
        return cid, "live"
    try:
        result = resolve(cid)
    except Exception:  # health: allow BLE001 -- fail-closed: a liveness-oracle fault is the stat_error posture (degrade + warn once), never a crash
        return cid, "stat_error"
    if result is UNCHECKABLE:
        return cid, "live"
    if result is None:
        return cid, "ghost"
    return cid, "live"


def is_degraded(posture: str) -> bool:
    """True for the two postures that must degrade + warn once (``ghost`` /
    ``stat_error``). ``live`` and ``first_contact`` are quiet."""
    return posture in DEGRADED_POSTURES


def resolve_to_id(cid: Optional[str], posture: str) -> Optional[str]:
    """The id a caller may USE: the live id, else ``None`` (degrade to today's
    behavior). Pure convenience over ``classify_home_category``'s result —
    never a second taxonomy, just the fall-through value for placement/routing.
    """
    return cid if posture == "live" else None
