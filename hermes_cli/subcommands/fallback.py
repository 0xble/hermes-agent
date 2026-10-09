"""``hermes fallback`` subcommand parser."""

from __future__ import annotations


def build_fallback_parser(subparsers) -> None:
    """Attach the ``fallback`` subcommand to ``subparsers``."""
    from hermes_cli.fallback_cmd import cmd_fallback

    fallback_parser = subparsers.add_parser(
        "fallback", help="Manage fallback providers (tried when the primary model fails)",
        description="Manage the fallback provider chain.  Fallback providers are tried "
            "in order when the primary model fails with rate-limit, overload, or "
            "connection errors.  See: "
            "https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers")
    fallback_subparsers = fallback_parser.add_subparsers(dest="fallback_command")
    fallback_subparsers.add_parser(
        "list", aliases=["ls"], help="Show the current fallback chain (default when no subcommand)")
    fallback_subparsers.add_parser(
        "add",
        help="Pick a provider + model (same picker as `hermes model`) and append to the chain")
    fallback_subparsers.add_parser(
        "remove", aliases=["rm"], help="Pick an entry to delete from the chain")
    fallback_subparsers.add_parser("clear", help="Remove all fallback entries")
    fallback_subparsers.add_parser(
        "status", help="Show shared primary-model cooldowns (same as `hermes fallback cooldowns`)")
    cooldowns_parser = fallback_subparsers.add_parser(
        "cooldowns", help="List shared primary-model cooldowns, or clear them with `cooldowns clear`")
    cooldowns_subparsers = cooldowns_parser.add_subparsers(dest="cooldowns_command")
    cooldowns_subparsers.add_parser("list", aliases=["ls"], help="List active cooldowns (default)")
    cooldowns_clear = cooldowns_subparsers.add_parser(
        "clear", help="Clear shared cooldowns; does not change the fallback chain")
    cooldowns_target = cooldowns_clear.add_mutually_exclusive_group(required=True)
    cooldowns_target.add_argument("--all", action="store_true", help="Clear every shared cooldown")
    cooldowns_target.add_argument(
        "target", nargs="?", help="Exact provider/model, or a model name to clear on every provider")
    fallback_parser.set_defaults(func=cmd_fallback)
