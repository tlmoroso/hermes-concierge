"""Home-category construction for the Discord concierge mixin: the leaf phase
that turns a guild + target member + visibility into a Discord permission
overwrite map.

Kept OUT of the facade (``concierge_mixin.py``): it is a pure builder over its
arguments, so it reads better — and ratchets — as its own unit.

``discord`` is imported at the call, as everywhere else in this package; any
fault there or in the guild reads propagates to the caller's fail-closed
``try``, which degrades to "no category" exactly as when this block was inline.

Returns a dict keyed by role/member, for ``guild.create_category(overwrites=)``.
"""


# A member who can VIEW a private home category must also be able to TALK in
# it. ``manage_channels`` does NOT imply ``send_messages``, and a private
# category denies @everyone outright — so with view+manage alone the member
# sees the channel and cannot post in it. Granted explicitly to the target
# member and to every administrator.
_TALK_PERMS = {
    "send_messages": True,
    "read_message_history": True,
    "create_public_threads": True,
    "create_private_threads": True,
    "manage_threads": True,
}


def _concierge_home_category_overwrites(guild, *, member, visibility):
    """Overwrites for a HOME CATEGORY about to be created in ``guild``.

    Kept here rather than inline in ``_concierge_ensure_home_category`` so the
    placement authority stays one readable flow; the extracted builder is its
    own ratchet unit.


    PRIVATE (``visibility != "public"``): @everyone is denied view; the target
    member and every guild administrator get view+manage+talk. PUBLIC: only
    the member's manage_channels overwrite.

    Either way the BOT's own overwrite is written LAST, so no branch above can
    drop it. A private category denies @everyone and grants only the target
    member plus guild administrators — the bot is neither — so without it the
    bot 403s on the home channel it just created ("Missing Access", observed
    live 2026-11-15).
    """
    import discord

    overwrites = {}
    if visibility != "public":
        overwrites[guild.default_role] = discord.PermissionOverwrite(
            view_channel=False)
        if member is not None:
            overwrites[member] = discord.PermissionOverwrite(
                view_channel=True, manage_channels=True, **_TALK_PERMS)
        for m in getattr(guild, "members", []) or []:
            perms = getattr(m, "guild_permissions", None)
            if getattr(perms, "administrator", False):
                overwrites[m] = discord.PermissionOverwrite(
                    view_channel=True, manage_channels=True, **_TALK_PERMS)
    elif member is not None:
        overwrites[member] = discord.PermissionOverwrite(
            manage_channels=True)
    bot_member = getattr(guild, "me", None)
    if bot_member is not None:
        overwrites[bot_member] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True)
    return overwrites
