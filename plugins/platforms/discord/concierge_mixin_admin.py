"""Admin home-space command handlers (/bot category, /bot space) for the Discord concierge (``concierge_mixin_admin.py`` sibling).

Whole methods moved out of ``concierge_mixin.py`` for the file-size ratchet;
moved code keeps its own per-unit cap (body_hash move pass). Mounted as a base
of ``DiscordAdapter`` next to ``ConciergeDiscordMixin`` / ``ConciergeVoiceMixin``:
duck-typed companions, so every ``self._concierge_*`` helper resolves through
the adapter MRO at call time. No re-export shims: internal paths are not API.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from plugins.platforms.discord.concierge_mixin import _concierge_payload

if TYPE_CHECKING:
    import discord

logger = logging.getLogger(__name__)


class ConciergeAdminMixin:
    async def _concierge_cmd_category(self, interaction, user: str) -> None:
        """``/bot category <user>`` — a PRIVATE home category (admin-only).

        A single-user category is private by implication (operator decision
        §12.5): it is named ``<display-name>`` — the old ``-private`` suffix is
        dropped — and the default role is denied view. The general channel,
        orchestrator provisioning, registry record and binding all live in
        ``_concierge_build_home_space``. Idempotent: a user who already has a
        resolving home category is adopted, never duplicated.
        """
        await self._concierge_defer(interaction, ephemeral=True)
        guild = getattr(interaction, "guild", None)
        if guild is None:
            await self._concierge_respond(
                interaction, "Must be used in a server.", ephemeral=True)
            return
        target_id = str(user or "").strip().lstrip("<@").rstrip(">").strip()
        try:
            target_id_int = int(target_id)
        except (TypeError, ValueError):
            await self._concierge_respond(
                interaction, "Invalid user id.", ephemeral=True)
            return
        try:
            member = (guild.get_member(target_id_int)
                      or await guild.fetch_member(target_id_int))
        except Exception:  # health: allow BLE001 -- fail-closed: an unresolvable guild member degrades the home-space name to the raw user id
            member = None
        name = getattr(member, "display_name", None) or target_id
        await self._concierge_build_home_space(
            interaction, guild=guild, user_id=target_id,
            name=name, member=member, visibility="private")

    async def _concierge_cmd_space(self, interaction, name: str) -> None:
        """``/bot space <name>`` — a PUBLIC shared category (admin-only).

        A shared space is not one user's private category (§12.5): it is named
        by the operator-supplied topic (never a display name) and carries NO
        default-role overwrite. With no ``<user>`` argument, the invoking admin
        is the subject whose orchestrator is provisioned/bound and whose home
        category is recorded; all downstream steps are otherwise identical to
        ``/bot category``.
        """
        await self._concierge_defer(interaction, ephemeral=True)
        guild = getattr(interaction, "guild", None)
        if guild is None:
            await self._concierge_respond(
                interaction, "Must be used in a server.", ephemeral=True)
            return
        topic = str(name or "").strip()
        if not topic:
            await self._concierge_respond(
                interaction, "Give the space a name.", ephemeral=True)
            return
        caller = getattr(interaction, "user", None)
        user_id = str(getattr(caller, "id", "") or "")
        if not user_id:
            await self._concierge_respond(
                interaction, "Could not resolve the caller.", ephemeral=True)
            return
        await self._concierge_build_home_space(
            interaction, guild=guild, user_id=user_id,
            name=topic, member=caller, visibility="public")

    async def _concierge_build_home_space(self, interaction, *, guild,
                                          user_id, name, member,
                                          visibility):
        """Create (or adopt) the subject's home category + its general channel.

        Both /bot category (private) and /bot space (public) funnel here. The
        DISCORD heavy lifting is delegated to CARD F's shared helpers — the
        category (private/public overwrites + a best-effort ``home_category``
        record) to ``_concierge_ensure_home_category`` and the general channel
        (create + greet + auto-offer suppression, config-driven name) to
        ``_concierge_ensure_general_channel``.

        The NON-DISCORD tail — ensure-orchestrator, record the FULL
        ``home_category`` + ``home_channel`` + ``guild_id`` triple, then
        ``bind_channel`` LAST — is delegated to the ONE shared pure helper
        ``concierge.home_space.provision_home_space`` that the offline CLI also
        calls (operator decision 2026-11-14). So the admin surface NEVER
        re-implements provisioning and the ordering/registry contract lives in
        exactly one place for both lanes.

        Order (HOME_CATEGORY_DESIGN §3.1), all best-effort and fail-closed:

        1. create the category (shared helper);
        2. create the GENERAL channel (shared helper, suppresses the
           channel-create auto-offer via ``_concierge_create_channel`` →
           ``_concierge_remember_hub_created_channel``);
        3–5. the shared helper: provision the orchestrator only if the user has
             none, record the FULL triple, ``bind_channel`` LAST — nothing
             routes into the channel until the binding lands.

        ``home_category`` is written by BOTH the category helper (best-effort,
        during creation) and the shared helper (authoritative FULL triple).
        The latter is needed because ``registry.update_user`` never resurrects
        a missing user: on a fresh admin creation the helper's early write
        NO-OPS (the users entry is created later by provisioning), so the FULL
        triple record is what guarantees all three fields land. The pre-card-B
        ``ValueError`` fallback is gone — card B is landed and
        ``home_category`` is a first-class field.

        Re-running for a user whose ``home_category`` still resolves ADOPTS the
        existing category/channel and reports both ids without creating a
        duplicate. The ephemeral reply always carries BOTH the category id and
        the general channel id.
        """
        from types import SimpleNamespace
        from concierge import registry

        guild_id = str(getattr(guild, "id", "") or "") or None

        # ── idempotency: adopt a still-resolving home category ──────────────
        existing = self._concierge_home_category_for(user_id)
        if existing and self._concierge_home_space_category_resolves(
                guild, existing):
            home_channel = registry.home_channel_for_user(user_id)
            await self._concierge_respond(
                interaction,
                f"Adopted existing home category (id {existing}) with general "
                f"channel (id {home_channel}).",
                ephemeral=True)
            return

        # ── 1) the category: CARD F shared helper ───────────────────────────
        # ``_concierge_ensure_home_category`` implements BOTH private and
        # public overwrite policies, adopts a still-resolving pointer, and
        # records home_category via update_user. It resolves the guild through
        # the hub's own client (self._client), so the interaction's guild is
        # used here only for the idempotency check above and the guild_id.
        category_id = await self._concierge_ensure_home_category(
            user_id=user_id, guild_id=guild_id,
            visibility=visibility, name=name)
        if not category_id:
            await self._concierge_respond(
                interaction, "Failed to create the home category.",
                ephemeral=True)
            return
        category_name = name

        # ── 2) the general channel: CARD F shared helper ────────────────────
        # ``_concierge_ensure_general_channel`` creates the channel via
        # ``_concierge_create_channel`` (which suppresses the auto-offer),
        # greets it, and names it through the ONE naming authority with
        # home_space=True (the config-driven "general" name, card A). ``source``
        # is source-shaped only for the (best-effort) greeting title; with
        # home_space=True the NAME is config-driven, not source-derived.
        source = SimpleNamespace(
            user_name=(getattr(member, "display_name", None)
                       or getattr(member, "name", None) or name),
            user_id=user_id)
        general_id = await self._concierge_ensure_general_channel(
            guild_id=guild_id, category_id=category_id, source=source,
            home_space=True)
        if not general_id:
            await self._concierge_respond(
                interaction,
                f"Created category '{category_name}' (id {category_id}) but "
                f"could not create its general channel.",
                ephemeral=True)
            return

        # ── 3–5) the NON-DISCORD tail is ONE shared pure helper ─────────────
        # Both lanes (this admin surface AND the offline CLI) call
        # ``concierge.home_space.provision_home_space``: ensure-orchestrator
        # (provision only when the user has none), record the FULL
        # home_category + home_channel + guild_id triple, then bind_channel
        # LAST. Card B landed, so the pre-card-B ValueError fallback is gone.
        # The helper reuses provision.create_bot + the registry writers; this
        # surface owns NO provisioning logic of its own.
        from concierge.home_space import HomeSpaceError, provision_home_space
        try:
            await asyncio.to_thread(
                provision_home_space,
                user_id=user_id, guild_id=guild_id,
                home_channel=general_id, home_category=category_id,
                blueprint="orchestrator")
        except HomeSpaceError as e:
            messages = {
                "provision": "Failed to provision the orchestrator",
                "record": "Failed to record the home space",
                "bind": "Failed to bind the general channel",
            }
            await self._concierge_respond(
                interaction,
                f"{messages.get(e.step, 'Failed to set up the home space')}: "
                f"{e.cause}",
                ephemeral=True)
            return

        await self._concierge_respond(
            interaction,
            f"Created {visibility} category '{category_name}' "
            f"(id {category_id}) with general channel (id {general_id}).",
            ephemeral=True)

    @staticmethod
    def _concierge_home_category_for(user_id):
        """Tolerant read of the subject's recorded home category. Prefers the
        registry accessor (card B) and falls back to the normalized user entry
        so this surface works against a registry that predates the new field.
        """
        try:
            from concierge import registry
            reader = getattr(registry, "home_category_for_user", None)
            if callable(reader):
                return reader(user_id)
            entry = registry.get_user(user_id) or {}
            return entry.get("home_category")
        except Exception:  # health: allow BLE001 -- fail-closed: an unreadable registry yields no home-category pointer for the admin picker
            return None

    def _concierge_home_space_category_resolves(self, guild,
                                                 category_id) -> bool:
        """True when *category_id* still names a live channel in the guild.
        Governs adopt-vs-recreate. When the guild/client offers no lookup we
        fail safe toward ADOPTING (never create a duplicate); a lookup that
        returns nothing means Discord invalidated the category -> recreate.
        Never raises.
        """
        cid = str(category_id or "")
        if not cid:
            return False
        client = getattr(self, "_client", None)
        getter = getattr(guild, "get_channel", None)
        if not callable(getter) and client is not None:
            getter = getattr(client, "get_channel", None)
        if not callable(getter):
            return True
        try:
            return getter(int(cid)) is not None
        except Exception:  # health: allow BLE001 -- fail-closed: a faulted int-coerced category lookup falls back to the raw-id lookup
            try:
                return getter(cid) is not None
            except Exception:  # health: allow BLE001 -- fail-closed: a faulted raw-id lookup resolves to no adoption (the category does not resolve)
                return False
