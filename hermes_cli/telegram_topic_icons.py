"""Operator CLI surface for custom Telegram topic icons."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from typing import Any


SCHEMA_VERSION = 1


def build_operator_service():
    from plugins.platforms.telegram.topic_icon_service import (
        build_profile_topic_icon_service,
    )

    return build_profile_topic_icon_service()


def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return asyncio.run(value)
    return value


def _emit(payload: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
    elif isinstance(payload, dict):
        for key, value in payload.items():
            print(f"{key}: {value}")
    else:
        print(payload)


def dispatch(args: argparse.Namespace) -> int:
    command = getattr(args, "telegram_topic_icon_command", None)
    as_json = bool(getattr(args, "json", False))
    if command == "set" and not getattr(args, "yes", False):
        print("Refusing topic mutation without explicit --yes.", file=sys.stderr)
        return 2

    if command == "status" and not getattr(args, "live", False):
        from hermes_cli.telegram import _local_status

        local = _local_status()["topic_icons"]
        payload = {"schema_version": SCHEMA_VERSION, "live": False, **local}
        _emit(payload, as_json=as_json)
        return 0

    try:
        service = build_operator_service()
        if command == "status":
            payload = _resolve(service.status(live=True))
        elif command == "catalog":
            payload = _resolve(
                service.catalog(
                    pack=getattr(args, "pack", None),
                    emoji=getattr(args, "emoji", None),
                    limit=getattr(args, "limit", 100),
                    refresh=bool(getattr(args, "refresh", False)),
                )
            )
        elif command == "resolve":
            payload = _resolve(
                service.resolve(args.emoji, pack=getattr(args, "pack", None))
            )
        elif command == "verify":
            payload = _resolve(service.verify(args.topic))
        elif command == "set":
            payload = _resolve(
                service.set(args.topic, args.emoji, pack=getattr(args, "pack", None))
            )
            if payload.get("requested_icon_emoji_id") != payload.get(
                "observed_icon_emoji_id"
            ):
                print("Telegram topic icon readback was not exact.", file=sys.stderr)
                return 1
        else:
            raise ValueError(f"unsupported topic-icons command: {command}")
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _emit(payload, as_json=as_json)
    return 0
