"""Fail-closed admission for plugin callbacks that can observe conversation content."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger("hermes_cli.plugins")


def _warning(message: str) -> None:
    # Privacy lookups can also be used by a logging record factory. Do not let
    # a broken factory turn a skipped write/dispatch into a failed user turn.
    try:
        logger.warning(message)
    except Exception:
        try:
            logger.handle(logging.LogRecord(logger.name, logging.WARNING, __file__, 0, message, (), None))
        except Exception:
            pass


def _session_ids(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _session_ids(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _session_ids(item)


def private_dispatch(payload: Any) -> bool | None:
    """Registry checks also cover callbacks invoked after the originating turn ends."""
    try:
        from agent.incognito import is_incognito

        return bool(is_incognito() or any(is_incognito(sid) for sid in _session_ids(payload)))
    except Exception:
        _warning("Plugin content dispatch skipped: privacy state unavailable")
        return None


def protect_execution_callback(manifest: Any, callback: Any):
    """Execution chains call the registered function directly, outside the manager."""
    from functools import wraps

    @wraps(callback)
    def invoke(**kwargs):
        if declared_dispatch_allowed(manifest.incognito_safe is True, kwargs):
            return callback(**kwargs)
        return kwargs["next_call"]()

    return invoke


def track_callback(manager: Any, manifest: Any, callback: Any, release: Any):
    """Keep callback identity unchanged and retire privacy ownership with its lease."""
    record = (callback, manifest)
    manager._incognito_callback_manifests.setdefault(id(callback), []).append(record)

    def dispose():
        release()
        owners = manager._incognito_callback_manifests.get(id(callback), [])
        manager._remove_identity(owners, record)
        if not owners:
            manager._incognito_callback_manifests.pop(id(callback), None)

    return dispose


def callback_allowed(manager: Any, callback: Any, payload: Any) -> bool:
    private = private_dispatch(payload)
    if private is False:
        return True
    owners = manager._incognito_callback_manifests.get(id(callback), ())
    if private is True and owners and all(cb is callback and manifest.incognito_safe is True for cb, manifest in owners):
        return True
    _warning("Plugin content dispatch skipped: incognito")
    return False


def declared_dispatch_allowed(incognito_safe: bool, payload: Any) -> bool:
    private = private_dispatch(payload)
    if private is False or (private is True and incognito_safe is True):
        return True
    _warning("Plugin content dispatch skipped: incognito")
    return False
