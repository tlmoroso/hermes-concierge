"""Room command handlers (/room create/add/remove/disband) for the Discord concierge (``concierge_mixin_room_commands.py`` sibling).

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


class ConciergeRoomCommandsMixin:
    async def _concierge_cmd_room_create(self, interaction,
                                         topic: str | None = None) -> None:
        """``/room create [topic]``: an ephemeral MULTI-SELECT of this guild's
        bound bots (2–6); the selection creates ``#room-<topic>`` and rooms
        it (Task 7.4). Fewer than two guild bots is actionable refusal — no
        picker at all."""
        from concierge import registry

        if not await self._concierge_gate_interaction(interaction, "/room create"):
            return
        guild_id = self._concierge_interaction_guild_id(interaction)
        choices = _concierge_payload("build_room_member_choices")(
            registry.list_bindings(), guild_id)
        if len(choices) < 2:
            await self._concierge_respond(
                interaction,
                "A room needs at least two bots — set more up with "
                "`/bot new` first, then try again.", ephemeral=True)
            return
        view = self._concierge_build_room_picker_view(choices, topic=topic)
        await self._concierge_respond(
            interaction,
            "Pick **2–6 bots** for the room — they'll all answer in the new "
            "channel.", view=view, ephemeral=True)

    def _concierge_build_room_picker_view(self, choices, *, topic=None):
        """The multi-select View for ``/room create``: ONE Select with
        ``min_values=2`` / ``max_values=MAX_ROOM_MEMBERS`` (discord.py
        selects are multi-select when the bounds allow it — the same View
        machinery the /bot picker uses, no modal needed). The topic threads
        into the select callback so the created channel is named after what
        the user asked for at command time."""
        import discord
        from concierge.registry import MAX_ROOM_MEMBERS

        view = discord.ui.View(timeout=300)
        options = [
            discord.SelectOption(
                label=c["label"][:100],
                value=c["value"],
                description=(c.get("description") or "")[:100],
            )
            for c in choices[:25]
        ]
        select = discord.ui.Select(
            placeholder="Pick this room's bots…", options=options,
            min_values=2, max_values=min(MAX_ROOM_MEMBERS, len(options)))
        select.callback = self._concierge_make_room_picker_callback(
            view, topic=topic)
        view.add_item(select)
        return view

    def _concierge_make_room_picker_callback(self, view, *, topic=None):
        """Bound callback for the room picker's multi-select: auth, defer
        (the channel create + registry write are slow), then create the room
        (see ``_concierge_create_room_from_interaction``)."""
        async def _on_select(interaction: discord.Interaction) -> None:
            if not self._concierge_component_auth(interaction):
                await interaction.response.send_message(
                    "You're not allowed to use this.", ephemeral=True)
                return
            await self._concierge_defer(interaction, ephemeral=True)
            values = [str(v) for v in
                      (getattr(interaction, "data", {}) or {}).get("values", [])]
            await self._concierge_create_room_from_interaction(
                interaction, values, topic=topic, source_view=view)

        return _on_select

    async def _concierge_create_room_from_interaction(
        self, interaction, values, *, topic=None, source_view=None,
    ) -> None:
        """The room-creation flow behind the picker: validate the selection
        (pure payload builder — 2–6 of THIS guild's bound bots), create
        ``#room-<topic>`` via the Task 5.7 channel-creation path, register
        the room on the NEW channel, and post the greeting naming the members
        into it.

        Unlike ``/bot new`` there is NO bind-the-invoking-channel fallback:
        a room NEEDS its own channel (plan), so a refused creation is a
        refusal with actionable copy — the invoking channel is never turned
        into a room. The registry mutation is a flock-guarded sync write:
        ``asyncio.to_thread`` (M5c discipline)."""
        from concierge import registry

        guild_id = self._concierge_interaction_guild_id(interaction)
        choices = _concierge_payload("build_room_member_choices")(
            registry.list_bindings(), guild_id)
        error = _concierge_payload("validate_room_selection")(values, choices)
        if error:
            await self._concierge_respond(interaction, error, ephemeral=True)
            return
        members = list(dict.fromkeys(str(v) for v in values))

        settings = self._concierge_settings_obj()
        category_id = self._concierge_room_category_id(interaction, settings)
        created = await self._concierge_create_channel(
            guild_id=guild_id, category_id=category_id,
            name=_concierge_payload("room_channel_name")(topic))
        if created is None:
            # The live 403 path ('Missing Permissions (50013)'): unlike
            # /bot new there is no bind-the-invoking-channel fallback here —
            # a room NEEDS its own channel, so the refusal is EPHEMERAL,
            # actionable, and leaves the registry untouched (no partial room).
            await self._concierge_respond(
                interaction,
                "I couldn't create a channel — grant me the **Manage "
                "Channels** permission (Server Settings → Roles), then run "
                "/room create again.",
                ephemeral=True)
            return
        try:
            # Off the loop: create_room is a flock-guarded sync write.
            await asyncio.to_thread(registry.create_room, created, members)
        except ValueError as e:
            # The registry is the authority (cap/duplicates); its message is
            # already user-appropriate.
            await self._concierge_respond(
                interaction, f"Couldn't create the room — {e}", ephemeral=True)
            return
        titles = [self._concierge_member_title(m) for m in members]
        await self._concierge_respond(
            interaction,
            f"✅ Room created in <#{created}> — "
            + ", ".join(f"**{t}**" for t in titles) + " will answer there.",
            ephemeral=True)
        await self._concierge_post_to_channel(
            created, _concierge_payload("build_room_created_response")(titles))
        if source_view is not None:
            try:
                for child in getattr(source_view, "children", []) or []:
                    child.disabled = True
            except Exception:
                pass
        logger.info(
            "[%s] concierge: created bot room on channel %s (%d members)",
            getattr(self, "name", "discord"), created, len(members))

    def _concierge_room_category_id(self, interaction, settings):
        """The managed category a room channel is created in: the invoking
        channel's category when that one is managed, else the first
        configured managed category, else ``None`` (the guild's top level —
        Discord accepts it and never guesses, same as we do)."""
        managed = [str(c) for c in (
            (settings.managed_category_ids if settings is not None else [])
            or [])]
        invoking = self._concierge_interaction_category_id(interaction)
        if invoking is not None and invoking in managed:
            return invoking
        return managed[0] if managed else None

    def _concierge_room_of_interaction(self, interaction) -> list[str]:
        """The room members of the channel the interaction was invoked IN
        (the /room add|remove|disband operand), or ``[]`` when that channel
        is not a room — callers answer the actionable refusal."""
        from concierge import registry

        channel_id = self._concierge_interaction_channel_id(interaction)
        if not channel_id:
            return []
        return registry.room_members(str(channel_id))

    async def _concierge_cmd_room_add(self, interaction, bot: str) -> None:
        """``/room add <bot>``: a guild-scoped bound bot joins the room of
        the INVOKING channel. Refusals: not-a-room (actionable copy), a bot
        that isn't this guild's; the registry's own ValueError (duplicate,
        full room) surfaces verbatim — it is the authority."""
        from concierge import registry

        if not await self._concierge_gate_interaction(interaction, "/room add"):
            return
        channel_id = self._concierge_interaction_channel_id(interaction)
        members = self._concierge_room_of_interaction(interaction)
        if not members:
            await self._concierge_respond(
                interaction,
                "This channel isn't a bot room — create one with "
                "`/room create` first.", ephemeral=True)
            return
        profile = str(bot or "").strip()
        guild_id = self._concierge_interaction_guild_id(interaction)
        choices = _concierge_payload("build_room_member_choices")(
            registry.list_bindings(), guild_id)
        if not any(c["value"] == profile for c in choices):
            await self._concierge_respond(
                interaction,
                f"`{profile}` isn't one of this server's bots — check "
                f"`/bot list`.", ephemeral=True)
            return
        await self._concierge_defer(interaction, ephemeral=True)
        try:
            # Off the loop: add_to_room is a flock-guarded sync write.
            new_members = await asyncio.to_thread(
                registry.add_to_room, str(channel_id), profile)
        except ValueError as e:
            await self._concierge_respond(
                interaction, f"Couldn't add it — {e}", ephemeral=True)
            return
        await self._concierge_respond(
            interaction,
            f"✅ **{self._concierge_member_title(profile)}** joined the "
            f"room.\n" + _concierge_payload("build_room_info_response")(
                [self._concierge_member_title(m) for m in new_members]),
            ephemeral=True)

    async def _concierge_cmd_room_remove(self, interaction, bot: str) -> None:
        """``/room remove <bot>``: drop a member from the invoking channel's
        room. The registry's own last-removal contract (the room disbands)
        is SURFACED in the reply copy, not hidden."""
        from concierge import registry

        if not await self._concierge_gate_interaction(interaction, "/room remove"):
            return
        channel_id = self._concierge_interaction_channel_id(interaction)
        members = self._concierge_room_of_interaction(interaction)
        if not members:
            await self._concierge_respond(
                interaction,
                "This channel isn't a bot room — create one with "
                "`/room create` first.", ephemeral=True)
            return
        profile = str(bot or "").strip()
        if profile not in members:
            await self._concierge_respond(
                interaction, f"`{profile}` isn't in this room.", ephemeral=True)
            return
        await self._concierge_defer(interaction, ephemeral=True)
        # Off the loop: remove_from_room is a flock-guarded sync write.
        removed = await asyncio.to_thread(
            registry.remove_from_room, str(channel_id), profile)
        members_after = registry.room_members(str(channel_id))
        title = self._concierge_member_title(profile)
        if removed and not members_after:
            await self._concierge_respond(
                interaction,
                f"🗑 Removed **{title}** — that was the last bot, so the "
                f"room is disbanded.", ephemeral=True)
        elif removed:
            await self._concierge_respond(
                interaction,
                f"🗑 Removed **{title}** from the room.\n"
                + _concierge_payload("build_room_info_response")(
                    [self._concierge_member_title(m) for m in members_after]),
                ephemeral=True)
        else:
            await self._concierge_respond(
                interaction, f"`{profile}` isn't in this room.", ephemeral=True)

    async def _concierge_cmd_room_disband(self, interaction) -> None:
        """``/room disband``: a confirmation button (destructive), then the
        registry-driven teardown — every member removed, the last removal
        deletes the room key (remove_from_room's own contract). The channel
        itself stays."""
        from concierge import registry

        if not await self._concierge_gate_interaction(interaction, "/room disband"):
            return
        channel_id = self._concierge_interaction_channel_id(interaction)
        members = self._concierge_room_of_interaction(interaction)
        if not members:
            await self._concierge_respond(
                interaction,
                "This channel isn't a bot room — create one with "
                "`/room create` first.", ephemeral=True)
            return
        titles = [self._concierge_member_title(m) for m in members]
        content = _concierge_payload("build_room_disband_response")(titles)

        import discord

        view = discord.ui.View(timeout=120)

        async def _disband_now(btn_interaction):
            if not self._concierge_component_auth(btn_interaction):
                await btn_interaction.response.send_message(
                    "You're not allowed to use this.", ephemeral=True)
                return
            # Defer BEFORE the teardown: registry writes under the flock.
            await self._concierge_defer(btn_interaction, ephemeral=True)
            try:
                await asyncio.to_thread(
                    self._concierge_disband_room_members, str(channel_id))
            except Exception as e:
                logger.error(
                    "[%s] concierge: disband of the room on channel %s "
                    "failed: %s",
                    getattr(self, "name", "discord"), channel_id, e,
                    exc_info=True)
                await self._concierge_respond(
                    btn_interaction,
                    "Couldn't disband it — try again in a moment.",
                    ephemeral=True)
                return
            try:
                for child in view.children:
                    child.disabled = True
            except Exception:
                pass
            await self._concierge_respond(
                btn_interaction,
                "🗑 Room disbanded — the channel stays; the bots no longer "
                "answer here.", ephemeral=True)

        async def _cancel(btn_interaction):
            try:
                for child in view.children:
                    child.disabled = True
            except Exception:
                pass
            await btn_interaction.response.send_message(
                "Cancelled — the room is untouched.", ephemeral=True)

        disband_btn = discord.ui.Button(
            label="Disband room", style=discord.ButtonStyle.danger)
        disband_btn.callback = _disband_now
        cancel_btn = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_btn.callback = _cancel
        view.add_item(disband_btn)
        view.add_item(cancel_btn)
        await self._concierge_respond(
            interaction, content, view=view, ephemeral=True)

    def _concierge_disband_room_members(self, channel_id: str) -> None:
        """Registry-driven room teardown (runs on a worker thread): remove
        every member; the LAST removal deletes the room key
        (remove_from_room's own contract — no second deletion path)."""
        from concierge import registry

        for member in list(registry.room_members(channel_id)):
            registry.remove_from_room(channel_id, member)

    # ── channel-delete auto-teardown ────────────────────────────────────────
