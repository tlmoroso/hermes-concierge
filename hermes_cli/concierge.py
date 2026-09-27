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

    settings = _settings()
    group = resolve_provider_group(
        settings.provider_groups,
        guild_id=args.guild,
        category_id=None,
        default_group=settings.default_provider_group,
    )
    try:
        binding = provision.create_bot(
            args.blueprint,
            channel_id=str(args.channel),
            guild_id=args.guild,
            provider_group=group,
            display_name=args.name,
        )
    except (KeyError, TimeoutError) as exc:
        # KeyError: unknown blueprint, or a provider_groups entry missing its
        # required keys (get_blueprint/apply_group_to_preset both raise with
        # operator-actionable messages). TimeoutError: another provision of
        # this channel is holding the per-channel lock. Both leave nothing
        # half-built (create_bot rolls itself back). Printed to stdout like
        # every sibling CLI's error copy (cron/webhook precedent).
        print(f"Error: {exc}")
        return 1

    from concierge.blueprints import get_blueprint

    bp = get_blueprint(binding["blueprint"])
    header = ("Already provisioned" if binding.get("already_provisioned")
              else "Created Concierge bot")
    print(f"\n  {header}: {bp.title}")
    print(f"  Profile: {binding['profile']}")
    print(f"  Channel: {args.channel}")
    print(f"  Blueprint: {binding['blueprint']}")
    if binding.get("provider_group"):
        print(f"  Provider group: {binding['provider_group']}")
    if bp.routine:
        print(f"  Routine: {bp.routine.name} ({bp.routine.schedule})")
    print()
    return 0


def _cmd_list(args) -> int:
    from concierge import provision, registry
    from concierge.blueprints import CATALOG

    bindings = registry.list_bindings()
    if not bindings:
        print("\n  No concierge bots yet.")
        print("  Create one with: hermes concierge new <blueprint> --channel <channel-id>")
        print(f"  Blueprints: {', '.join(CATALOG)}\n")
        return 0

    by_profile: dict = {}
    for channel_id, binding in bindings.items():
        by_profile.setdefault(binding.get("profile"), []).append((channel_id, binding))

    print(f"\n  {len(by_profile)} concierge bot(s):\n")
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


_ACTIONS = {
    "new": _cmd_new, "create": _cmd_new,
    "list": _cmd_list, "ls": _cmd_list,
    "retire": _cmd_retire,
    "bindings": _cmd_bindings,
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
              "Usage: hermes concierge [new|list|retire|bindings]")
        return 1
    return handler(args)
