"""CLI lifecycle for the optional profile-local Telegram user session."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from typing import Any

from hermes_constants import get_hermes_home
from plugins.platforms.telegram.user_transport import (
    IMPLEMENTED_CAPABILITIES,
    TelegramUserTransportConfig,
    TelegramUserTransportError,
    inspect_safe_session_storage,
    telegram_user_session_path,
)


def _configured() -> TelegramUserTransportConfig:
    from hermes_cli.config import load_config
    from hermes_cli.telegram import _telegram_extra

    config = load_config()
    extra = _telegram_extra(config if isinstance(config, dict) else {})
    return TelegramUserTransportConfig.from_mapping(extra.get("user_transport"))


def local_diagnostics(status: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    try:
        config = _configured()
        checks.append({"name": "config_valid", "ok": True})
    except TelegramUserTransportError as exc:
        checks.append({"name": "config_valid", "ok": False, "detail": str(exc)})
        config = None
    checks.append(
        {
            "name": "dependency_available",
            "ok": importlib.util.find_spec("telethon") is not None,
        }
    )
    session = telegram_user_session_path(get_hermes_home())
    checks.append({"name": "session_present", "ok": session.is_file()})
    try:
        inspect_safe_session_storage(get_hermes_home())
        checks.append({"name": "session_storage_safe", "ok": True})
    except TelegramUserTransportError as exc:
        checks.append(
            {"name": "session_storage_safe", "ok": False, "detail": str(exc)}
        )
    if config is not None:
        checks.append({"name": "transport_enabled", "ok": config.enabled})
    return checks


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    for key, value in payload.items():
        print(f"{key}: {value}")


def _secret(name: str) -> str:
    from agent.secret_scope import get_secret

    return str(get_secret(name, "") or "").strip()


def _login() -> dict[str, Any]:
    from plugins.platforms.telegram.mtproto_telethon import authorize_attended

    config = _configured()
    raw_api_id = _secret("TELEGRAM_API_ID")
    api_hash = _secret("TELEGRAM_API_HASH")
    try:
        api_id = int(raw_api_id)
    except (TypeError, ValueError) as exc:
        raise TelegramUserTransportError(
            "TELEGRAM_API_ID is missing or invalid in the active profile"
        ) from exc
    if not api_hash:
        raise TelegramUserTransportError(
            "TELEGRAM_API_HASH is missing in the active profile"
        )
    identity = asyncio.run(
        authorize_attended(
            config=config,
            api_id=api_id,
            api_hash=api_hash,
            hermes_home=get_hermes_home(),
        )
    )
    return {
        "schema_version": 1,
        "authorized": True,
        "user_id": identity.user_id,
        "username": identity.username,
        "premium": identity.is_premium,
    }


def run_setup(_args: argparse.Namespace) -> int:
    print("Telegram configuration is read from platforms.telegram.extra.")
    print("Authorize the optional user transport with: hermes telegram user login")
    return 0


def dispatch(args: argparse.Namespace) -> int:
    command = getattr(args, "telegram_user_command", None)
    as_json = bool(getattr(args, "json", False))
    try:
        config = _configured()
        if command == "capabilities":
            configured = sorted(config.capabilities)
            payload = {
                "schema_version": 1,
                "configured": configured,
                "implemented": sorted(IMPLEMENTED_CAPABILITIES),
                "effective": configured if config.enabled else [],
            }
        elif command == "peers":
            payload = {
                "schema_version": 1,
                "live": False,
                "peers": [
                    {"peer_id": peer_id, "verified": None}
                    for peer_id in sorted(config.allowed_bot_peer_ids)
                ],
            }
            if getattr(args, "live", False):
                from plugins.platforms.telegram.topic_icon_runtime import (
                    ProfileTopicIconService,
                )

                payload = asyncio.run(ProfileTopicIconService().peers())
        elif command in {"status", "doctor"}:
            checks = local_diagnostics()
            payload = {
                "schema_version": 1,
                "live": False,
                "enabled": config.enabled,
                "session_present": telegram_user_session_path(
                    get_hermes_home()
                ).is_file(),
                "checks": checks,
            }
            if getattr(args, "live", False):
                from plugins.platforms.telegram.topic_icon_runtime import (
                    ProfileTopicIconService,
                )

                payload = asyncio.run(ProfileTopicIconService().user_status())
        elif command == "login":
            payload = _login()
        elif command == "logout":
            from plugins.platforms.telegram.mtproto_telethon import logout_attended

            raw_api_id = _secret("TELEGRAM_API_ID")
            api_hash = _secret("TELEGRAM_API_HASH")
            payload = asyncio.run(
                logout_attended(
                    config=config,
                    api_id=int(raw_api_id),
                    api_hash=api_hash,
                    hermes_home=get_hermes_home(),
                )
            )
        else:
            raise TelegramUserTransportError("unsupported Telegram user command")
    except (TelegramUserTransportError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _emit(payload, as_json=as_json)
    return 0
