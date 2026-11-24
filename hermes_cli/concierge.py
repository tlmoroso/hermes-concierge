"""hermes concierge — operator CLI for Concierge bots (new/list/retire/bindings).

Thin argparse surface over the provisioning service (concierge/provision.py)
and the binding registry (concierge/registry.py); this module owns NO
provisioning logic of its own. Mirrors hermes_cli/cron.py and webhook.py:
a ``concierge_command(args)`` entry point dispatched from a ``_ACTIONS`` map,
int exit codes (0 = success, 1 = operator-visible failure) returned to
``main()`` via ``forward_return=True``, and heavy imports kept INSIDE the
handlers so importing this module stays cheap on the parser path.

Config: provider groups are read through ``load_config()`` (the subcommand
loader, per hermes_cli/AGENTS.md's three-loader rule) + concierge_settings.
The ``concierge.enabled`` flag is deliberately NOT gated here — it switches
the Discord surface; an operator explicitly running ``hermes concierge new``
has already opted in (and the acceptance flow runs in a home with no
config.yaml at all).

Provider-group resolution passes ``category_id=None``: the channel's Discord
category is only knowable from the interaction (the gateway path), so the CLI
matches on guild -> default, never category.

M4b review fixes encoded here:
- ``retire`` refuses anything that is not a Concierge bot's profile: the
  argument is operator-supplied free text, and the old ``profile_exists``
  check guarded EXISTENCE, not identity — it happily retired ``default``
  (the operator's own hub home; ``profile_exists("default")`` is trivially
  True) or any plain profile. A bot is exactly one of: a profile the
  concierge registry has a binding for, or a ``bot-``-prefixed profile
  (registry membership covers normal operation; the prefix covers a bot
  orphaned mid-retire, whose binding is gone — precisely the state a manual
  retire exists to finish). ``default`` is refused outright; a plain
  non-``bot-`` profile with no binding is refused even when it exists.
- ``new`` prints "Already provisioned" (and says so truthfully) when
  ``create_bot`` returns the channel's existing bot instead of creating one —
  the old "Created Concierge bot" header was a lie on the idempotent path,
  with no signal to distinguish the two outcomes.

T7 (orchestrator redesign) encoded here:
- ``new`` targets a channel, a user, or both: ``--user <discord_user_id>``
  provisions a user-owned CHANNEL-LESS bot (the per-user orchestrator and
  workers); ``--channel`` keeps the v1 channel-bound semantics; the
  neither-flag refusal moved OFF the parser and INTO the handler so it is
  an operator-actionable message + exit 1, and the handler's except clause
  additionally catches ValueError (T3: a channel-less worker without a user
  registry entry — provision their orchestrator first).
- ``list`` prints a users section first (per user: orchestrator profile,
  home channel or "(pending first contact)", workers ``blueprint →
  profile``) — a channel-less bot has no binding, so the per-profile
  bindings section (unchanged) can never list it. ``bindings`` stays the
  flat channel table (design §7 says so explicitly).

Card I (home-category CLI, HOME_CATEGORY_DESIGN §3.2) encoded here:
- ``new orchestrator --user X`` gains ``--category <id>`` (bind to an
  EXISTING category) and ``--public`` (create a shared public one named
  by ``--name``). Both are legal ONLY with ``--user`` — ``--category`` +
  ``--channel`` and ``--category`` without ``--user`` are refused with a
  short operator message + exit 1; the ``--channel``/``--user`` target rule
  is unchanged.
- Home-space mode delegates the NON-DISCORD tail to ONE shared pure helper,
  ``concierge.home_space.provision_home_space`` (the SAME helper card H's
  ``/bot category`` / ``/bot space`` admin surface calls): ensure the
  orchestrator (provision only when the user has none), record
  ``home_category`` + ``home_channel`` + ``guild_id``, then ``bind_channel``
  LAST. The CLI owns NO provisioning/registry logic of its own. The
  general-channel name comes from the shared ``home_general_channel_name``
  (card-A config-driven, default ``general``). Discord object ids come from
  ``_create_home_space_channel`` (the CLI's single Discord seam; offline it
  records deterministic ids the runtime reconciles, design §6).
"""

from __future__ import annotations


def _settings():
    """Concierge settings from the subcommand config loader (call-time import
    keeps this module cheap to import on the parser path)."""
    from concierge.settings import concierge_settings
    from hermes_cli.config import load_config

    return concierge_settings(load_config())


def _title(blueprint_key):
    """Blueprint title for display; the raw key if it left the catalog."""
    from concierge.blueprints import get_blueprint

    try:
        return get_blueprint(str(blueprint_key)).title
    except KeyError:
        return str(blueprint_key)


def _cmd_new(args) -> int:
    from concierge.provider_groups import resolve_provider_group
    from concierge import provision

    # T7 (design §7): a provision targets a CHANNEL (v1 channel-bound bot),
    # a USER (user-owned, channel-less bot), or both. The parser no longer
    # requires --channel so this refusal can be an operator-actionable
    # message instead of argparse's bare usage error.
    channel_id = str(args.channel) if args.channel is not None else None
    user_id = str(args.user) if args.user is not None else None
    category_id = (str(args.category)
                   if getattr(args, "category", None) is not None else None)
    public = bool(getattr(args, "public", False))
    # Home-space flags (--category / --public) are legal ONLY with --user:
    # --category binds an EXISTING category for that user, --public creates a
    # shared one. These are operator-actionable refusals BEFORE any provision.
    # --channel + --user together remains legal, as before. --category + --channel
    # is explicitly refused (checked first: it also lacks --user).
    if category_id is not None and channel_id is not None:
        print("Error: --category cannot be used with --channel — it belongs "
              "with --user <discord-user-id> to place the orchestrator's home "
              "space. Use --channel alone for a channel-bound bot.")
        return 1
    if category_id is not None and user_id is None:
        print("Error: --category needs a --user <discord-user-id> — it binds "
              "an existing home category for that user and cannot stand alone.")
        return 1
    if public and user_id is None:
        print("Error: --public needs a --user <discord-user-id> — it creates "
              "a shared public home category for that user.")
        return 1
    # The neither-flag refusal stays the operator-actionable fallback (T7).
    if channel_id is None and user_id is None:
        print("Error: a target is required: pass --channel <channel-id> "
              "(channel-bound bot) or --user <discord-user-id> "
              "(user-owned, channel-less bot)")
        return 1

    settings = _settings()
    group = resolve_provider_group(
        settings.provider_groups,
        guild_id=args.guild,
        category_id=None,
        default_group=settings.default_provider_group,
    )

    # Home-space mode: delegate to the SAME provisioning + registry helpers
    # the /bot category + /bot space admin surface (card H) uses. The CLI owns
    # NO provisioning logic of its own — it calls provision.create_bot and the
    # registry writers below, it never re-implements them.
    if category_id is not None or public:
        return _cmd_new_home_space(
            args, user_id=user_id, guild_id=args.guild, group=group,
            category_id=category_id, public=public, settings=settings)

    try:
        result = provision.create_bot(
            args.blueprint,
            channel_id=channel_id,
            user_id=user_id,
            guild_id=args.guild,
            provider_group=group,
            display_name=args.name,
        )
    except (KeyError, TimeoutError, ValueError) as exc:
        # KeyError: unknown blueprint, or a provider_groups entry missing its
        # required keys (get_blueprint/apply_group_to_preset both raise with
        # operator-actionable messages). TimeoutError: another provision of
        # this channel or user is holding the per-target lock. ValueError
        # (T3): a channel-less WORKER provision for a user with no registry
        # entry — their orchestrator must be provisioned first. All leave
        # nothing half-built (create_bot rolls itself back) and are
        # operator-actionable, printed to stdout like every sibling CLI's
        # error copy (cron/webhook precedent).
        print(f"Error: {exc}")
        return 1

    from concierge import registry
    from concierge.blueprints import get_blueprint

    # The return shape depends on the mode: channel-bound provisions return
    # the binding; a channel-less orchestrator returns the users entry (its
    # profile lives under the "orchestrator" key), a channel-less worker
    # {"profile", "blueprint"}. The honest header covers them all.
    profile = result.get("profile") or result.get("orchestrator")
    bp = get_blueprint(args.blueprint)
    header = ("Already provisioned" if result.get("already_provisioned")
              else "Created Concierge bot")
    print(f"\n  {header}: {bp.title}")
    print(f"  Profile: {profile}")
    if channel_id is not None:
        print(f"  Channel: {channel_id}")
    if user_id is not None:
        print(f"  User: {user_id}")
    print(f"  Blueprint: {bp.key}")
    if result.get("provider_group"):
        print(f"  Provider group: {result['provider_group']}")
    if bp.routine:
        print(f"  Routine: {bp.routine.name} ({bp.routine.schedule})")
    if user_id is not None:
        # The user's home channel: known once the bootstrap flow created it,
        # else it materializes on the user's first Discord contact (the
        # channel-less bot has nothing to print on the Channel line).
        home_channel = registry.home_channel_for_user(user_id)
        if home_channel:
            print(f"  Home channel: {home_channel}")
        else:
            print("  Home channel: personal channel is created on first "
                  "Discord contact")
    print()
    return 0


def _create_home_space_channel(*, user_id, guild_id, category_id, name,
                               public, general_channel_name):
    """The CLI's single Discord-facing seam for a home space.

    A home space is a Discord CATEGORY plus its GENERAL channel. In a live
    deployment (hub gateway present) this delegates to the very same channel /
    category creation path the ``/bot category`` and ``/bot space`` admin
    commands use (card H) and returns the REAL Discord ids. The operator CLI
    runs offline (no gateway), so by default it records stable, deterministic
    ids that the runtime reconciles against Discord (design §6); a test or an
    embedding deployment can monkeypatch this function to inject real ids. The
    CLI itself never issues Discord API calls — it only ever works with id
    strings, exactly as ``provision.create_bot`` does.
    """
    import hashlib

    def _offline_id(kind, *parts):
        seed = ":".join(str(p) for p in (kind, user_id, guild_id, *parts))
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
        return str(int(digest[:16], 16))

    visibility = "public" if public else "private"
    if public:
        # A shared space is named by the operator-supplied topic, never by a
        # user's display name (§12.5). --category, when also supplied, names an
        # EXISTING public category; otherwise a new one is created.
        space_name = (str(name).strip() if name else "") or f"shared-{user_id}"
        cat_id = (str(category_id) if category_id is not None
                  else _offline_id("category", space_name))
    else:
        # A single-user category is private by implication; --category is the
        # id of the EXISTING category to place the general channel inside.
        cat_id = str(category_id)
        space_name = (str(name).strip() if name else "") or cat_id
    general_id = _offline_id("channel", cat_id, general_channel_name)
    return {"category_id": cat_id, "channel_id": general_id,
            "name": space_name, "visibility": visibility,
            "general_channel_name": general_channel_name}


def _cmd_new_home_space(args, *, user_id, guild_id, group, category_id,
                        public, settings) -> int:
    """``new orchestrator --user X`` with ``--category <id>`` / ``--public``.

    Establishes the user's home space. The Discord-facing part (resolving /
    creating the category + its general channel) is the CLI's single seam
    ``_create_home_space_channel``; everything NON-DISCORD — ensure the
    orchestrator, record ``home_category`` + ``home_channel`` + ``guild_id``,
    ``bind_channel`` LAST — is delegated to the ONE shared pure helper
    ``concierge.home_space.provision_home_space`` that the ``/bot category`` /
    ``/bot space`` admin surface (card H) also calls. The CLI owns NO
    provisioning or registry logic of its own (operator decision 2026-11-14).
    """
    from concierge.blueprints import get_blueprint
    from concierge.home_space import (
        HomeSpaceError, home_general_channel_name, provision_home_space,
    )

    # ONE pure function decides the HOME-space general-channel name (card-A
    # config-driven, default ``general``) — the same function the mixin's
    # single naming authority calls.
    general_channel_name = home_general_channel_name(settings)

    # 1) resolve/create the category + its general channel (single seam).
    space = _create_home_space_channel(
        user_id=user_id, guild_id=guild_id, category_id=category_id,
        name=args.name, public=public,
        general_channel_name=general_channel_name)
    cat_id = space["category_id"]
    general_id = space["channel_id"]

    # 2) the shared, non-Discord tail: ensure-orchestrator -> record the FULL
    #    triple -> bind_channel LAST. Both lanes call this ONE helper.
    try:
        result = provision_home_space(
            user_id=user_id, guild_id=guild_id,
            home_channel=general_id, home_category=cat_id,
            blueprint=args.blueprint,
            provider_group=group, display_name=args.name)
    except HomeSpaceError as exc:
        print(f"Error: {exc}")
        return 1
    profile = result["profile"]
    already = bool(result["already_provisioned"])

    # Report — always carries BOTH ids, the visibility, and the space name.
    bp = get_blueprint(args.blueprint)
    header = "Already provisioned" if already else "Created Concierge bot"
    print(f"\n  {header}: {bp.title}")
    print(f"  Profile: {profile}")
    print(f"  User: {user_id}")
    print(f"  Blueprint: {bp.key}")
    print(f"  Home category: {cat_id} ({space['visibility']}, "
          f"name '{space['name']}')")
    print(f"  General channel: {general_id} "
          f"(name '{general_channel_name}')")
    if guild_id is not None:
        print(f"  Guild: {guild_id}")
    print()
    return 0


def _cmd_list(args) -> int:
    from concierge import provision, registry
    from concierge.blueprints import CATALOG

    bindings = registry.list_bindings()
    users = registry.list_users()
    if not bindings and not users:
        print("\n  No concierge bots yet.")
        print("  Create one with: hermes concierge new <blueprint> --channel <channel-id>")
        print(f"  Blueprints: {', '.join(CATALOG)}\n")
        return 0

    # Users section (T7, design §7) — printed FIRST. A user-owned bot is
    # channel-less: it has NO binding, so the per-profile section below can
    # never list it; the users entry is its only registry record.
    if users:
        print(f"\n  {len(users)} user(s):")
        for user_id, entry in sorted(users.items()):
            orchestrator = entry.get("orchestrator")
            if orchestrator:
                print(f"\n  ◆ User {user_id} — orchestrator {orchestrator}")
            else:
                print(f"\n  ◆ User {user_id} — no orchestrator")
            home_channel = entry.get("home_channel")
            if home_channel:
                print(f"    Home channel: {home_channel}")
            else:
                print("    Home channel: (pending first contact)")
            for worker_key, worker_profile in sorted(
                    (entry.get("workers") or {}).items()):
                print(f"    Worker: {worker_key} → {worker_profile}")
        print()

    by_profile: dict = {}
    for channel_id, binding in bindings.items():
        by_profile.setdefault(binding.get("profile"), []).append((channel_id, binding))

    if by_profile:
        # v1 byte-parity: the bot section opens with a blank line when no
        # users section was printed above (v1 output); the users section
        # already ends with one, so no double blank in the combined case.
        print(("\n" if not users else "")
              + f"  {len(by_profile)} concierge bot(s):\n")
        for profile, entries in by_profile.items():
            print(f"  ◆ {_title(entries[0][1].get('blueprint'))} ({profile})")
            for channel_id, binding in entries:
                guild = f" (guild {binding['guild_id']})" if binding.get("guild_id") else ""
                print(f"    Channel: {channel_id}{guild}")
            status = provision.bot_status(profile)
            for routine in status.get("routines", []):
                state = "paused" if routine.get("paused") else "scheduled"
                print(f"    Routine: {routine['name']} — {state}")
            print()
    return 0


def _is_concierge_bot_profile(profile: str) -> bool:
    """True only for a Concierge bot's profile — the ONLY kind ``retire`` may
    touch. Registry membership is the primary test (a binding names the bot's
    profile); a ``bot-``-prefixed name is the fallback for a bot orphaned
    mid-retire — its binding is already gone, which is exactly the state a
    manual retire exists to finish. ``default`` is never a bot (it is the
    operator's hub home, not a provisioned profile)."""
    from concierge import provision, registry

    if str(profile).strip().casefold() == "default":
        return False
    if registry.channels_for_profile(profile):
        return True
    return str(profile).startswith(provision._PROFILE_PREFIX + "-")


def _cmd_retire(args) -> int:
    from hermes_cli.profiles import profile_exists
    from concierge import provision, registry

    profile = args.profile
    # IDENTITY guard, before existence: the argument is free-text operator
    # input, and profile_exists() alone would pass `default` (trivially True)
    # or any plain human profile. retire pauses routines, unbinds channels and
    # (with --delete) runs the canonical profile teardown — none of that may
    # ever run against a profile this service did not provision.
    if not _is_concierge_bot_profile(profile):
        print(f"Error: '{profile}' is not a Concierge bot profile.")
        print("Bots are listed by profile name in: hermes concierge list")
        return 1
    if not profile_exists(profile):
        print(f"Error: no bot profile named '{profile}'.")
        print("List bots with: hermes concierge list")
        return 1

    # Capture the channels before the retire unbinds them (for the report).
    channels = registry.channels_for_profile(profile)
    delete = bool(getattr(args, "delete", False))
    provision.retire_bot(profile, delete_profile=delete)

    print(f"\n  Retired Concierge bot: {profile}")
    print(f"  Unbound channel(s): {', '.join(channels) if channels else '(none)'}")
    print("  Routine(s): paused")
    if delete:
        print("  Profile: deleted")
    else:
        print(f"  Profile: kept (remove for good with: hermes concierge retire {profile} --delete)")
    print()
    return 0


def _cmd_bindings(args) -> int:
    from concierge import registry

    bindings = registry.list_bindings()
    if not bindings:
        print("\n  No channel bindings.")
        print("  Create one with: hermes concierge new <blueprint> --channel <channel-id>\n")
        return 0

    print(f"\n  {len(bindings)} channel binding(s):\n")
    for channel_id, binding in sorted(bindings.items()):
        guild = f", guild: {binding['guild_id']}" if binding.get("guild_id") else ""
        print(f"  {channel_id} -> {binding.get('profile')} "
              f"(blueprint: {binding.get('blueprint')}{guild})")
    print()
    return 0


def _cmd_refresh_souls(args) -> int:
    """Deploy-time repair: SOUL.md is written once at provisioning, so a
    catalog/SOUL edit never reaches an already-live bot. Re-render every
    registered bot's SOUL.md from the CURRENT catalog (persona names kept).
    """
    from concierge import provision

    updated = provision.refresh_souls()
    if updated:
        print(f"✓ refreshed persona/toolsets for {len(updated)} bot(s):")
        for name in updated:
            print(f"  - {name}")
    else:
        print("✓ all bot SOUL.md files and toolsets already match the current "
              "catalog")
    print("Restart the gateway so running sessions pick up the new personas.")
    return 0


_ACTIONS = {
    "new": _cmd_new, "create": _cmd_new,
    "list": _cmd_list, "ls": _cmd_list,
    "retire": _cmd_retire,
    "bindings": _cmd_bindings,
    "refresh-souls": _cmd_refresh_souls,
}


def concierge_command(args) -> int:
    """Entry point for the ``hermes concierge`` subcommand.

    Bare ``hermes concierge`` means ``list`` (cron_command parity). Returns
    the process exit code: 0 on success, 1 on an operator-visible failure;
    unexpected internal errors propagate with their traceback (an operator
    tool must not swallow what it cannot explain).
    """
    sub = getattr(args, "concierge_action", None) or "list"
    handler = _ACTIONS.get(sub)
    if handler is None:
        print(f"Unknown concierge command: {sub}\n"
              "Usage: hermes concierge [new|list|retire|bindings|refresh-souls]")
        return 1
    return handler(args)
