"""Hermes-specific Telegram administration commands.

The namespace is intentionally narrower than a Telegram client: Bot API
messaging remains owned by the gateway and the optional user transport exposes
only forum-topic reads and icon writes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from hermes_cli.config import load_config


SCHEMA_VERSION = 1


def _telegram_platform(config: dict[str, Any]) -> dict[str, Any]:
    """Merge canonical gateway config with the legacy top-level override."""
    merged: dict[str, Any] = {}
    merged_extra: dict[str, Any] = {}
    raw_gateway = config.get("gateway")
    gateway: dict[str, Any] = raw_gateway if isinstance(raw_gateway, dict) else {}
    raw_gateway_platforms = gateway.get("platforms")
    gateway_platforms = (
        raw_gateway_platforms if isinstance(raw_gateway_platforms, dict) else {}
    )
    raw_top_platforms = config.get("platforms")
    top_platforms = raw_top_platforms if isinstance(raw_top_platforms, dict) else {}
    sources = (gateway_platforms, top_platforms)
    for platforms in sources:
        telegram = platforms.get("telegram")
        if not isinstance(telegram, dict):
            continue
        extra = telegram.get("extra")
        merged.update({key: value for key, value in telegram.items() if key != "extra"})
        if isinstance(extra, dict):
            merged_extra.update(extra)
    if merged_extra:
        merged["extra"] = merged_extra
    return merged


def _telegram_extra(config: dict[str, Any]) -> dict[str, Any]:
    extra = _telegram_platform(config).get("extra")
    return extra if isinstance(extra, dict) else {}


def _user_transport_config(extra: dict[str, Any]) -> dict[str, Any]:
    value = extra.get("user_transport")
    return value if isinstance(value, dict) else {}


def _local_status() -> dict[str, Any]:
    config = load_config()
    telegram = _telegram_platform(config if isinstance(config, dict) else {})
    extra = _telegram_extra(config)
    bot_enabled = bool(telegram.get("enabled", False))
    bot_configured = bool(telegram.get("token"))
    try:
        from gateway.config import Platform, load_gateway_config

        effective = load_gateway_config().platforms.get(Platform.TELEGRAM)
        if effective is not None:
            bot_enabled = bool(effective.enabled)
            bot_configured = bool(effective.token)
            if isinstance(effective.extra, dict):
                extra = effective.extra
    except Exception:
        # Local status must remain available for malformed or incomplete
        # gateway configuration. Focused doctor reports the validation error.
        pass
    user = _user_transport_config(extra)
    packs = extra.get("topic_icon_custom_packs")
    if not isinstance(packs, list):
        packs = []
    capabilities = user.get("capabilities")
    if not isinstance(capabilities, list):
        capabilities = []
    peers = user.get("allowed_bot_peer_ids")
    if not isinstance(peers, list):
        peers = []
    return {
        "schema_version": SCHEMA_VERSION,
        "live": False,
        "bot_api": {
            "enabled": bot_enabled,
            "configured": bot_configured,
        },
        "user_transport": {
            "enabled": bool(user.get("enabled", False)),
            "implementation": str(user.get("implementation") or "telethon"),
            "expected_user_id": user.get("expected_user_id"),
            "require_premium": bool(user.get("require_premium", True)),
            "capabilities": list(capabilities),
            "allowed_bot_peer_ids": list(peers),
            "connect_timeout_seconds": user.get("connect_timeout_seconds", 15),
            "rpc_timeout_seconds": user.get("rpc_timeout_seconds", 20),
            "max_flood_wait_seconds": user.get("max_flood_wait_seconds", 5),
            "failure_cooldown_seconds": user.get("failure_cooldown_seconds", 300),
        },
        "topic_icons": {
            "enabled": bool(extra.get("auto_topic_icons", False)),
            "provider": str(extra.get("topic_icon_provider") or "telegram_default"),
            "preserve_manual": bool(extra.get("preserve_manual_topic_icons", True)),
            "configured_packs": list(packs),
            "fallback": "none",
        },
    }


def build_live_service():
    """Construct the live operator service lazily.

    Keeping this import boundary explicit guarantees that local status and
    doctor never import Telethon or touch a session.
    """
    from hermes_cli.telegram_topic_icons import build_operator_service

    return build_operator_service()


def _run(value):
    return asyncio.run(value) if hasattr(value, "__await__") else value


def _merge_live_status(payload: dict[str, Any], live: dict[str, Any]) -> None:
    payload["live"] = True
    topic_icons = payload.setdefault("topic_icons", {})
    topic_icons.update(
        {
            key: value
            for key, value in live.items()
            if key
            in {
                "provider",
                "fallback",
                "configured_packs",
                "per_pack_counts",
                "total_count",
                "cache_age_seconds",
            }
        }
    )
    user = payload.setdefault("user_transport", {})
    user.update(
        {
            "user_id": live.get("user_id"),
            "premium": live.get("premium"),
            "authorized": live.get("user_id") is not None,
        }
    )


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    print("Telegram")
    print(f"  Bot API: {'enabled' if payload['bot_api']['enabled'] else 'disabled'}")
    user = payload["user_transport"]
    print(f"  User transport: {'enabled' if user['enabled'] else 'disabled'}")
    icons = payload["topic_icons"]
    print(f"  Topic icons: {icons['provider']} (fallback: {icons['fallback']})")


def cmd_status(args: argparse.Namespace) -> int:
    payload = _local_status()
    if getattr(args, "live", False):
        try:
            live = _run(build_live_service().status(live=True))
            _merge_live_status(payload, live)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 1
    _emit(payload, as_json=bool(getattr(args, "json", False)))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from hermes_cli.telegram_user import local_diagnostics

    payload = _local_status()
    payload["checks"] = local_diagnostics(payload)
    if getattr(args, "live", False):
        try:
            live = _run(build_live_service().status(live=True))
            _merge_live_status(payload, live)
            payload["checks"].append({"name": "live_ready", "ok": True})
        except Exception as exc:
            payload["live"] = True
            payload["checks"].append(
                {"name": "live_ready", "ok": False, "detail": str(exc)}
            )
    payload["ok"] = all(check.get("ok", False) for check in payload["checks"])
    _emit(payload, as_json=bool(getattr(args, "json", False)))
    return 0 if payload["ok"] else 1


def cmd_setup(args: argparse.Namespace) -> int:
    requested = any(
        (
            getattr(args, "enable_user_transport", False),
            getattr(args, "expected_user_id", None),
            getattr(args, "bot_peer_id", None),
            getattr(args, "enable_custom_icons", False),
            getattr(args, "pack", None),
        )
    )
    if requested:
        expected_user_id = getattr(args, "expected_user_id", None)
        bot_peer_id = getattr(args, "bot_peer_id", None)
        packs = list(dict.fromkeys(getattr(args, "pack", None) or []))
        if getattr(args, "enable_user_transport", False) and not (
            expected_user_id and bot_peer_id
        ):
            print(
                "--enable-user-transport requires --expected-user-id and --bot-peer-id",
                file=sys.stderr,
            )
            return 2
        if getattr(args, "enable_custom_icons", False) and not packs:
            print("--enable-custom-icons requires at least one --pack", file=sys.stderr)
            return 2
        if getattr(args, "enable_custom_icons", False) and not getattr(
            args, "enable_user_transport", False
        ):
            print(
                "--enable-custom-icons requires --enable-user-transport",
                file=sys.stderr,
            )
            return 2
        user_transport = {
            "enabled": bool(getattr(args, "enable_user_transport", False)),
            "implementation": "telethon",
            "expected_user_id": expected_user_id,
            "require_premium": True,
            "capabilities": ["topic.read", "topic.icon.write"],
            "allowed_bot_peer_ids": [bot_peer_id] if bot_peer_id else [],
        }
        extra: dict[str, Any] = {"user_transport": user_transport}
        if getattr(args, "enable_custom_icons", False):
            extra.update(
                {
                    "auto_topic_icons": True,
                    "preserve_manual_topic_icons": True,
                    "topic_icon_provider": "telegram_custom_packs",
                    "topic_icon_custom_packs": packs,
                    "topic_icon_catalog_ttl_seconds": 86400,
                }
            )
        from hermes_cli.config import save_config

        save_config(
            {"gateway": {"platforms": {"telegram": {"extra": extra}}}},
            merge_existing=True,
            preserve_keys={
                ("gateway", "platforms", "telegram", "extra", "user_transport"),
                ("gateway", "platforms", "telegram", "extra", "auto_topic_icons"),
                ("gateway", "platforms", "telegram", "extra", "topic_icon_provider"),
                ("gateway", "platforms", "telegram", "extra", "topic_icon_custom_packs"),
            },
        )
        print("Telegram integration configuration updated.")
        print("Authorize the user transport with: hermes telegram user login")
        return 0
    if getattr(args, "non_interactive", False):
        print(
            "Telegram user authorization is attended. Run "
            "`hermes telegram user login` in a local terminal."
        )
        return 2
    from hermes_cli.telegram_user import run_setup

    return run_setup(args)


def _dispatch_user(args: argparse.Namespace) -> int:
    from hermes_cli.telegram_user import dispatch

    return dispatch(args)


def _dispatch_topic_icons(args: argparse.Namespace) -> int:
    from hermes_cli.telegram_topic_icons import dispatch

    return dispatch(args)


def _add_read_flags(parser: argparse.ArgumentParser, *, live: bool = True) -> None:
    if live:
        parser.add_argument("--live", action="store_true", help="Run bounded network checks")
    parser.add_argument("--json", action="store_true", help="Emit stable JSON output")


def register_cli(subparsers) -> argparse.ArgumentParser:
    telegram = subparsers.add_parser(
        "telegram",
        help="Administer the Hermes Telegram integration",
        description="Configure and inspect Hermes Telegram topic-icon support.",
    )
    commands = telegram.add_subparsers(dest="telegram_command")
    telegram.set_defaults(func=lambda _args: (telegram.print_help(), 0)[1])

    setup = commands.add_parser("setup", help="Configure Telegram integration")
    setup.add_argument("--non-interactive", action="store_true")
    setup.add_argument("--enable-user-transport", action="store_true")
    setup.add_argument("--expected-user-id", type=int)
    setup.add_argument("--bot-peer-id", type=int)
    setup.add_argument("--enable-custom-icons", action="store_true")
    setup.add_argument("--pack", action="append", default=[])
    setup.set_defaults(func=cmd_setup)

    status = commands.add_parser("status", help="Show Telegram integration status")
    _add_read_flags(status)
    status.set_defaults(func=cmd_status)

    doctor = commands.add_parser("doctor", help="Diagnose Telegram integration")
    _add_read_flags(doctor)
    doctor.set_defaults(func=cmd_doctor)

    user = commands.add_parser("user", help="Manage the optional Telegram user session")
    user_commands = user.add_subparsers(dest="telegram_user_command")
    user.set_defaults(func=lambda _args: (user.print_help(), 0)[1])
    for name, help_text in (
        ("login", "Authorize an attended profile-local session"),
        ("status", "Show user-session status"),
        ("doctor", "Diagnose user-session readiness"),
        ("logout", "Revoke and remove the profile-local session"),
        ("capabilities", "Show configured and effective capabilities"),
        ("peers", "Show configured allowed bot peers"),
    ):
        command = user_commands.add_parser(name, help=help_text)
        if name in {"status", "doctor", "peers"}:
            _add_read_flags(command)
        elif name == "capabilities":
            _add_read_flags(command, live=False)
        command.set_defaults(func=_dispatch_user)

    icons = commands.add_parser("topic-icons", help="Inspect and set custom topic icons")
    icon_commands = icons.add_subparsers(dest="telegram_topic_icon_command")
    icons.set_defaults(func=lambda _args: (icons.print_help(), 0)[1])

    icon_status = icon_commands.add_parser("status", help="Show topic-icon status")
    _add_read_flags(icon_status)
    icon_status.set_defaults(func=_dispatch_topic_icons)

    catalog = icon_commands.add_parser("catalog", help="List the custom-pack catalog")
    catalog.add_argument("--pack")
    catalog.add_argument("--emoji")
    catalog.add_argument("--limit", type=int, default=100)
    catalog.add_argument("--refresh", action="store_true")
    catalog.add_argument("--json", action="store_true")
    catalog.set_defaults(func=_dispatch_topic_icons)

    resolve = icon_commands.add_parser("resolve", help="Resolve Unicode to a custom icon")
    resolve.add_argument("emoji")
    resolve.add_argument("--pack")
    resolve.add_argument("--json", action="store_true")
    resolve.set_defaults(func=_dispatch_topic_icons)

    verify = icon_commands.add_parser("verify", help="Read and classify one exact topic")
    verify.add_argument("--topic", type=int, required=True)
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(func=_dispatch_topic_icons)

    set_icon = icon_commands.add_parser("set", help="Set one custom icon with exact readback")
    set_icon.add_argument("--topic", type=int, required=True)
    set_icon.add_argument("--emoji", required=True)
    set_icon.add_argument("--pack")
    set_icon.add_argument("--yes", action="store_true")
    set_icon.add_argument("--json", action="store_true")
    set_icon.set_defaults(func=_dispatch_topic_icons)
    return telegram
