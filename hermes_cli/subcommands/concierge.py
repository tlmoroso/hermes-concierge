"""``hermes concierge`` subcommand parser."""

from __future__ import annotations

from typing import Callable


def build_concierge_parser(subparsers, *, cmd_concierge: Callable) -> None:
    """Attach the ``concierge`` subcommand to ``subparsers``.

    Mirrors ``build_cron_parser`` / ``build_webhook_parser``: the handler is
    dependency-injected so this module never imports ``hermes_cli.main``
    (cycle avoidance), and ``set_defaults(func=...)`` is what ``main()``'s
    ``args.func(args)`` dispatch keys off.
    """
    parser = subparsers.add_parser(
        "concierge",
        help="Manage Concierge bots (Discord per-channel helper bots)",
        description=(
            "Provision and retire Concierge bots: opinionated helper profiles "
            "bound to a Discord channel, with their routine and persona set "
            "up from a blueprint catalog."
        ),
    )
    sub = parser.add_subparsers(dest="concierge_action")

    new_sub = sub.add_parser(
        "new", aliases=["create"], help="Provision a bot from a blueprint into a channel"
    )
    new_sub.add_argument("blueprint", help="Blueprint key (see `hermes concierge list`)")
    new_sub.add_argument(
        "--channel", required=True,
        help="Discord channel id the bot is bound to (its routine posts here)"
    )
    new_sub.add_argument("--guild", default=None, help="Discord guild id (provider-group matching)")
    new_sub.add_argument(
        "--name", default=None, dest="name",
        help="Display name — becomes the bot's persona identity (SOUL {name})"
    )

    sub.add_parser("list", aliases=["ls"], help="List provisioned bots")

    retire_sub = sub.add_parser("retire", help="Retire a bot: pause its routine, unbind its channel(s)")
    retire_sub.add_argument("profile", help="Bot profile name (see `hermes concierge list`)")
    retire_sub.add_argument(
        "--delete", action="store_true",
        help="Also delete the bot's profile (default: keep it — reversible retire)"
    )

    sub.add_parser("bindings", help="Raw channel -> bot binding table")

    parser.set_defaults(func=cmd_concierge)
