"""Durable inbound spooling before cancellable gateway shutdown phases."""
from __future__ import annotations

import logging
from collections.abc import Mapping

logger = logging.getLogger("gateway.run")


def _spool_slot(runner, mapping, key, value) -> int:
    try:
        if not runner._flush_owned_pending(key, value, reason="shutdown", overflow=False):
            return 0
        # A replacement arriving during the write still belongs to the next drain.
        if mapping.get(key) is value:
            remover = getattr(mapping, "pop", None)
            if callable(remover):
                remover(key, None)
        return 1
    except Exception:
        logger.exception("Failed to durably spool shutdown-pending message for %s", key)
        return 0


def _spool_mapping(runner, mapping) -> int:
    if not isinstance(mapping, Mapping):
        return 0
    return sum(_spool_slot(runner, mapping, key, value) for key, value in list(mapping.items()))


def _spool_overflow(runner) -> int:
    persisted = 0
    for key, state in list((runner.__dict__.get("_sessions") or {}).items()):
        events = state.conversation.queued_events
        if not events:
            continue
        try:
            if runner._flush_owned_pending(key, list(events), reason="shutdown", overflow=True):
                if state.conversation.queued_events is events:
                    state.conversation.queued_events = []
                persisted += 1
        except Exception:
            logger.exception("Failed to durably spool shutdown-pending message for %s", key)
    return persisted


def _spool_startup(runner) -> int:
    queue = getattr(runner, "_startup_restore_queue", None)
    if not isinstance(queue, list):
        return 0
    persisted = 0
    for event in list(queue):
        spool = getattr(event, "_hermes_recovery_spool", None)
        if spool is not None and spool.exists():
            continue
        try:
            key = runner._session_key_for_source(event.source)
            if runner._flush_owned_pending(key, event, reason="restore_shutdown"):
                _remove_by_identity(queue, event)
                persisted += 1
        except Exception:
            logger.exception("Failed to preserve startup-restore queued event during shutdown")
    return persisted


def _remove_by_identity(queue, event) -> None:
    for index, current in enumerate(queue):
        if current is event:
            del queue[index]
            break


def persist_shutdown_pending_messages(runner) -> int:
    """Spool each owned slot, clearing only the identity that actually reached disk."""
    persisted = _spool_mapping(runner, getattr(runner, "_pending_messages", None))
    persisted += _spool_overflow(runner)
    mappings = [getattr(runner, "adapters", {})]
    mappings.extend(getattr(runner, "_profile_adapters", {}).values())
    for mapping in mappings:
        for adapter in mapping.values():
            persisted += _spool_mapping(runner, getattr(adapter, "_pending_messages", None))
    persisted += _spool_startup(runner)
    if persisted:
        logger.info("Shutdown phase: durably spooled %d queued inbound message(s) before cleanup", persisted)
    return persisted
