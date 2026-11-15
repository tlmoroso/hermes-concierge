"""Provisioning + channel/thread creation handlers (/bot new) for the Discord concierge (``concierge_mixin_provisioning.py`` sibling).

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


class ConciergeProvisioningMixin:
    async def _concierge_cmd_new(self, interaction) -> None:
        """Ephemeral blueprint picker; the selection provisions the bot into
        the invoking channel (Task 5.4), via the Task 5.7 create-vs-bind plan
        when the invoking context is a managed category."""
        import discord

        if not await self._concierge_gate_interaction(interaction, "/bot new"):
            return
        payload = _concierge_payload("build_new_bot_response")()
        view = self._concierge_build_picker_view(payload["choices"])
        try:
            await interaction.response.send_message(
                payload["content"], view=view, ephemeral=True)
        except Exception as e:
            logger.warning("[%s] /bot new: picker send failed: %s",
                           getattr(self, "name", "discord"), e)

    def _concierge_build_picker_view(self, choices, *,
                                     prefer_create: bool = True,
                                     ephemeral: bool = True):
        """The blueprint select-menu View. Auth: the hub's user/role allowlists
        (transport-scoped, same as every component view in this adapter).

        ``prefer_create``/``ephemeral`` thread through the select callback
        into ``_concierge_provision_from_interaction``. The defaults are the
        ``/bot new`` semantics (a dedicated channel, ephemeral replies); the
        Task 5.5 auto-offer sites pass ``False``/``False`` — the channel the
        picker is posted in IS the destination, and the confirmation is
        public."""
        import discord

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
            placeholder="Pick a bot for this channel…", options=options)
        select.callback = self._concierge_make_picker_callback(
            view, prefer_create=prefer_create, ephemeral=ephemeral)
        view.add_item(select)
        return view

    def _concierge_make_picker_callback(self, view, *,
                                        prefer_create: bool = True,
                                        ephemeral: bool = True):
        """Bound callback for the picker's select: resolve the selection into
        a provisioned bot, threading the picker site's
        ``prefer_create``/``ephemeral`` into the provision step (see
        ``_concierge_build_picker_view``). Wrapped (not a View subclass) so
        the mixin stays duck-typed — ``_component_check_auth`` lives on the
        adapter module."""
        import discord

        async def _on_select(interaction: discord.Interaction) -> None:
            if not self._concierge_component_auth(interaction):
                await interaction.response.send_message(
                    "You're not allowed to use this.", ephemeral=True)
                return
            # Defer BEFORE any provisioning work: the provision (profile
            # copytree, cron-store write, a cross-process flock that can wait
            # up to 60s) can outlast the ~3s interaction token, and an
            # unanswered interaction burns it — defer-then-followup keeps
            # every outcome (success, error, lock timeout) deliverable. The
            # ephemeral flag threads from the picker site, so the auto-offer
            # pickers defer (and confirm) publicly, same as before.
            await self._concierge_defer(interaction, ephemeral=ephemeral)
            blueprint_key = str(
                (getattr(interaction, "data", {}) or {}).get("values", [""])[0])
            await self._concierge_provision_from_interaction(
                interaction, blueprint_key, source_view=view,
                ephemeral=ephemeral, prefer_create=prefer_create)

        return _on_select

    def _concierge_component_auth(self, interaction) -> bool:
        """User/role gate for concierge component clicks (picker, confirm
        buttons). Uses the adapter module's own ``_component_check_auth``
        (the same gate the adapter's approval/model-picker views use) with
        THIS adapter's allowlist sets; a bare/duck-typed adapter in tests may
        still provide ``_component_check_auth`` as an attribute, which wins."""
        checker = getattr(self, "_component_check_auth", None)
        if callable(checker):
            try:
                return bool(checker(
                    interaction,
                    getattr(self, "_allowed_user_ids", set()),
                    getattr(self, "_allowed_role_ids", set())))
            except Exception:
                return False
        # Upstream's approval-card refactor moved ``_component_check_auth`` out of
        # adapter.py into adapter_component_auth.py — resolve it there (still the
        # same gate the adapter's approval/model-picker views use).
        try:
            from plugins.platforms.discord.adapter_component_auth import _component_check_auth
            return bool(_component_check_auth(
                interaction,
                getattr(self, "_allowed_user_ids", set()),
                getattr(self, "_allowed_role_ids", set())))
        except Exception:
            return False

    async def _concierge_provision_from_interaction(
        self, interaction, blueprint_key: str, *, source_view=None,
        ephemeral: bool = True, prefer_create: bool = False,
    ) -> None:
        """Provision ``blueprint_key`` for the interaction's channel, through
        the Task 5.7 create-vs-bind plan. Shared by the ``/bot new`` picker
        (ephemeral replies, ``prefer_create=True``) and the Task 5.5
        auto-offer pickers (non-ephemeral, ``prefer_create=False`` — the
        channel the picker was posted in IS the destination); both flags are
        threaded from the picker site through
        ``_concierge_build_picker_view``/``_concierge_make_picker_callback``.
        When the plan CREATES a channel and provisions into it, a
        non-ephemeral greeting is posted there afterwards (plan 5.7: "channel
        appears, bot says hi") — best effort, never a provisioning failure.

        Failure copy is user-language ("couldn't set that up — try again"),
        never a Hermes stack trace: provisioning errors are logged with full
        detail on the hub side."""
        from concierge.provider_groups import resolve_provider_group

        channel_id = self._concierge_interaction_channel_id(interaction)
        guild_id = self._concierge_interaction_guild_id(interaction)
        category_id = self._concierge_interaction_category_id(interaction)
        settings = self._concierge_settings_obj()

        plan = self._concierge_plan_channel(
            channel_id=channel_id, category_id=category_id, settings=settings,
            guild_id=guild_id, prefer_create=prefer_create)

        created_channel_id = None
        try:
            if plan.mode == "create":
                created = await self._concierge_create_channel(
                    guild_id=guild_id, category_id=category_id,
                    name=_concierge_payload("channel_name_for_blueprint")(blueprint_key))
                if created is None:
                    # Fallback is the plan's own rule: never fail provisioning
                    # over channel creation.
                    if channel_id is None:
                        await self._concierge_respond(
                            interaction, "Couldn't create a channel for it — "
                            "run /bot new inside a channel instead.", ephemeral=ephemeral)
                        return
                    plan = _concierge_payload("ChannelPlan")(
                        "bind", reason="channel creation refused — binding the invoking channel")
                else:
                    channel_id = created
                    created_channel_id = created
            if channel_id is None:
                await self._concierge_respond(
                    interaction, "Couldn't tell which channel you're in — run "
                    "/bot new inside a channel.", ephemeral=ephemeral)
                return

            group = None
            if settings is not None:
                group = resolve_provider_group(
                    settings.provider_groups, guild_id=guild_id,
                    category_id=category_id,
                    default_group=settings.default_provider_group)

            from concierge import provision

            # Off the loop: create_bot does profile-tree creation, a skills
            # copytree, .env copy + adapter discovery, a cron-store write and
            # the registry bind — all under a cross-process flock that can
            # wait up to 60s. Synchronously on the discord.py event loop it
            # freezes ALL gateway event handling (a 60s wait misses the WS
            # heartbeat and forces a reconnect). to_thread is safe here: the
            # per-channel provision lock serializes concurrent callers and
            # create_bot's idempotence re-check happens INSIDE that lock; the
            # result is a plain dict, and every discord.py touch below stays
            # on the loop.
            binding = await asyncio.to_thread(
                provision.create_bot, blueprint_key,
                channel_id=str(channel_id), guild_id=guild_id,
                provider_group=group,
                user_id=str(getattr(getattr(interaction, "user", None), "id", "") or "") or None)
            title = self._concierge_blueprint_title(blueprint_key)
            already = bool(binding.get("already_provisioned"))
            message = (
                f"✅ **{title}** is already live in <#{channel_id}>!"
                if already
                else f"✅ **{title}** is live in <#{channel_id}> — say hi!")
            await self._concierge_respond(interaction, message, ephemeral=ephemeral)
            if created_channel_id is not None:
                # Plan 5.7: the bot says hi IN the channel it now lives in —
                # the ephemeral reply above is visible to the clicker only.
                await self._concierge_greet_created_channel(
                    created_channel_id, title)
            elif (plan.mode == "bind" and not already
                    and self._concierge_channel_is_thread(
                        getattr(interaction, "channel", None))):
                # Forum-post provisioning (a Discord forum post IS a thread):
                # the live 2026-09-27 bug — a BIND into a thread never posted
                # the confirmation INTO the thread, because the greeting
                # branch above is gated on channel creation. Post it through
                # the same best-effort channel-send path, then seed the newly
                # bound bot with the post's opening content. Fresh
                # provisions only (``already``): a re-selection of an
                # existing binding must not re-run the seed turn.
                await self._concierge_greet_thread(interaction, title)
            if source_view is not None:
                try:
                    for child in getattr(source_view, "children", []) or []:
                        child.disabled = True
                except Exception:
                    pass
            logger.info(
                "[%s] concierge: %s bot %r for channel %s (plan=%s)",
                getattr(self, "name", "discord"),
                "returned existing" if already else "provisioned",
                binding.get("profile"), channel_id, plan.mode)
        except Exception as e:
            logger.error(
                "[%s] concierge: /bot new provisioning failed for channel %s: %s",
                getattr(self, "name", "discord"), channel_id, e, exc_info=True)
            await self._concierge_respond(
                interaction,
                "Couldn't set that up — try again in a moment.",
                ephemeral=ephemeral)

    def _concierge_plan_channel(self, *, channel_id, category_id, settings,
                                guild_id=None, prefer_create: bool = False):
        """Task 5.7 decision: create a channel or bind the invoking one. The
        permission half of ``create_vs_bind`` is the MANAGE_CHANNELS
        pre-check on the hub's guild object, fed the REAL ``guild_id`` from
        the interaction/channel context (``None`` here would always take the
        unknowable→True branch and pre-check nothing). Best effort by design:
        a denied create still falls back to bind at run time — the API
        denial remains the enforcement of last resort.

        ``prefer_create=True`` is the ``/bot new`` flow (a fresh channel per
        bot in bot country); the Task 5.5 auto-offer sites pass False (the
        posted-in channel is the user's fresh canvas — bind it)."""
        can_create = self._concierge_can_create_channels(guild_id=guild_id)
        managed = settings.managed_category_ids if settings is not None else []
        return _concierge_payload("create_vs_bind")(
            can_create_channels=can_create,
            category_id=category_id,
            managed_category_ids=managed,
            channel_id=channel_id,
            prefer_create=prefer_create,
        )

    def _concierge_can_create_channels(self, *, guild_id) -> bool:
        """Best-effort MANAGE_CHANNELS check on the hub's guild object; True
        conservatively when unknowable (a denied create falls back to bind —
        failing closed here would silently disable the feature for bots whose
        permissions object isn't cached)."""
        client = getattr(self, "_client", None)
        guild = None
        if client is not None and guild_id is not None:
            try:
                guild = client.get_guild(int(guild_id)) if hasattr(client, "get_guild") else None
            except Exception:
                guild = None
            if guild is None and hasattr(client, "guilds"):
                try:
                    guild = next(
                        (g for g in client.guilds if str(getattr(g, "id", "")) == str(guild_id)),
                        None)
                except Exception:
                    guild = None
        me = getattr(guild, "me", None) if guild is not None else None
        perms = getattr(me, "guild_permissions", None) if me is not None else None
        manage = getattr(perms, "manage_channels", None) if perms is not None else None
        if manage is None:
            return True  # unknowable: let Discord itself referee the create call
        return bool(manage)

    async def _concierge_create_channel(self, *, guild_id, category_id, name):
        """Hub-side ``#<bot-name>`` creation (Task 5.7). Returns the new
        channel id, or ``None`` when creation is impossible/denied — callers
        fall back to binding the invoking channel. Best-effort by design:
        provisioning must never fail over channel creation."""
        client = getattr(self, "_client", None)
        if client is None or guild_id is None:
            return None
        guild = None
        try:
            if hasattr(client, "get_guild"):
                guild = client.get_guild(int(guild_id))
            if guild is None and hasattr(client, "guilds"):
                guild = next(
                    (g for g in client.guilds if str(getattr(g, "id", "")) == str(guild_id)),
                    None)
        except Exception:
            guild = None
        if guild is None:
            return None
        try:
            import discord

            kwargs = {"name": name}
            if category_id is not None:
                # A known category id (the managed one) is passed through;
                # discord.py accepts the raw id for category=.
                category = None
                try:
                    category = client.get_channel(int(category_id))
                except Exception:
                    category = None
                if category is not None:
                    kwargs["category"] = category
            created = await guild.create_text_channel(**kwargs)
            channel_id = str(getattr(created, "id", ""))
            # Suppress the create event's own auto-offer: this channel was
            # created BY the hub (both /room create and /bot new's create
            # plan ride this one helper), and the creating flow posts its
            # own greeting/confirmation into it.
            self._concierge_remember_hub_created_channel(channel_id)
            return channel_id
        except Exception as e:
            logger.warning(
                "[%s] concierge: channel creation failed (%s); falling back to "
                "binding the invoking channel",
                getattr(self, "name", "discord"), e)
            return None

    async def _concierge_greet_created_channel(self, channel_id,
                                               title: str) -> None:
        """Plan 5.7's "bot says hi": a non-ephemeral greeting posted into a
        channel the hub just created — delegated to
        ``_concierge_post_to_channel`` (best effort, never fails the caller;
        see that helper for the posture)."""
        await self._concierge_post_to_channel(
            channel_id, f"✅ **{title}** is live here — say hi!")

    async def _concierge_greet_thread(self, interaction, title: str) -> None:
        """The forum-post greeting (the thread-bind counterpart of
        ``_concierge_greet_created_channel``): post the confirmation INTO
        the thread through the same best-effort send, then seed the newly
        bound bot with the post's opening content.

        Why both ride here: a forum post's body is written at creation —
        BEFORE the binding existed — so it can never arrive as a routed
        message; without an explicit seed the new bot sits silent until the
        user types a second message (the live 2026-09-27 bug). The seed
        reuses the room fan-out's member-turn machinery (fresh source
        stamped with the bound profile, dispatched through the REAL
        ``super().handle_message``) — NO second turn mechanism. Best
        effort, error-isolated: neither a greeting failure nor a seed
        failure may fail the provisioning that already succeeded."""
        channel_id = self._concierge_interaction_channel_id(interaction)
        await self._concierge_post_to_channel(
            channel_id, f"✅ **{title}** is live here — say hi!",
            channel=getattr(interaction, "channel", None))
        await self._concierge_seed_thread_opening(interaction, channel_id)

    async def _concierge_seed_thread_opening(self, interaction,
                                             channel_id) -> None:
        """One seed turn for a freshly thread-bound bot, carrying the post's
        opening message (the starter message the user wrote at post
        creation — content that predates the binding and therefore can
        never reach the bot as a routed message).

        The turn rides the EXISTING dispatch: a source built through the
        REAL ``build_source`` (the concierge routing seam stamps the bound
        profile exactly as for any later thread message — the registry
        binding was just written), a MessageEvent carrying the starter
        content, dispatched via ``handle_message`` — canonicalization,
        profile-namespaced session key, busy guard and the runner's
        profile runtime scope all run as for any bound-channel message,
        and the reply lands in the thread through the same delivery path
        (the faaf55aac2 cross-profile origin-delivery fix). Spawns the same
        way the room fan-out driver does (returns fast, work on a task),
        then waits bounded for the seed turn so provisioning completes.
        Never raises: a seed failure is logged, never a provisioning one."""
        try:
            thread = getattr(interaction, "channel", None)
            if thread is None or channel_id is None:
                return
            # The post's opening content: the starter message the thread
            # carries (discord.py forum threads expose it as
            # ``starter_message``; older surfaces fetch by id). Missing
            # content means there is nothing to seed — the greeting already
            # told the user to say hi.
            content = None
            starter = getattr(thread, "starter_message", None)
            if starter is not None:
                content = getattr(starter, "content", None)
            if not (content or "").strip():
                logger.debug(
                    "[%s] concierge: forum post %s has no opening message "
                    "to seed; the greeting stands alone",
                    getattr(self, "name", "discord"), channel_id)
                return

            from gateway.platforms.event import MessageEvent, MessageType

            channel_name = getattr(thread, "name", "") or ""
            parent = getattr(thread, "parent", None)
            parent_id = (str(getattr(parent, "id", "") or "")
                         if parent is not None else None)
            source = self.build_source(
                chat_id=str(channel_id), chat_name=channel_name,
                chat_type="thread",
                user_id=str(getattr(interaction, "user_id", "")
                            or getattr(getattr(interaction, "user", None),
                                      "id", "") or ""),
                user_name=getattr(getattr(interaction, "user", None),
                                  "display_name", None),
                thread_id=str(channel_id),
                guild_id=self._concierge_interaction_guild_id(interaction),
                parent_chat_id=parent_id,
            )
            event = MessageEvent(
                text=str(content), message_type=MessageType.TEXT,
                source=source, raw_message=thread,
            )
            task = asyncio.create_task(self.handle_message(event))
            background = getattr(self, "_background_tasks", None)
            if background is not None:
                try:
                    background.add(task)
                    task.add_done_callback(background.discard)
                except Exception:
                    pass
            logger.info(
                "[%s] concierge: seeded the forum post %s opening content "
                "into the newly bound bot",
                getattr(self, "name", "discord"), channel_id)
        except Exception as e:
            logger.warning(
                "[%s] concierge: could not seed the forum post's opening "
                "content for channel %s; the bot stays available for the "
                "next message: %s",
                getattr(self, "name", "discord"), channel_id, e)


    async def _concierge_post_to_channel(self, channel_id, text: str,
                                         channel=None) -> None:
        """Best-effort non-ephemeral post into a channel (the created-channel
        greeting, the forum-post greeting and the room greeting share it):
        the destination resolved from an in-hand ``channel`` object when the
        caller holds one (the thread-bind path does — the interaction's
        channel IS the thread), else through the hub's own client. Never
        raises, and a post that cannot be sent is logged and skipped — the
        command that triggered it already succeeded."""
        if channel is None:
            client = getattr(self, "_client", None)
            if client is not None and channel_id is not None \
                    and hasattr(client, "get_channel"):
                try:
                    channel = client.get_channel(int(channel_id))
                except Exception:
                    channel = None
        try:
            if channel is None or not hasattr(channel, "send"):
                logger.warning(
                    "[%s] concierge: channel %s is not reachable for a "
                    "planned post; skipped it (the command itself succeeded)",
                    getattr(self, "name", "discord"), channel_id)
                return
            await channel.send(text)
        except Exception as e:
            logger.warning(
                "[%s] concierge: could not post into channel %s (%s); the "
                "command itself succeeded",
                getattr(self, "name", "discord"), channel_id, e)

    # ── Task 7.4: /room create | add | remove | disband ─────────────────────
