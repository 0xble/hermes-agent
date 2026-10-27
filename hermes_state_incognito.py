"""Privacy admission for state.db transactions, before SQLite sees any content."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)


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


def _identifiers(value: Any):
    """Accept IDs and batched/imported routing records without stringifying content."""
    if isinstance(value, str):
        yield value
        if value.startswith(("{", "[")):
            try:
                decoded = json.loads(value)
            except (ValueError, TypeError):
                return
            yield from _identifiers(decoded)
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _identifiers(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _identifiers(item)


@contextmanager
def write_admission(session_ids: Any = ()):
    """Serialize a committed write with flag changes, including worker callbacks.

    The registry's RLock also protects register_session. Holding it through COMMIT
    means an on-boundary cannot race between the privacy check and a disk write.
    """
    try:
        from agent.incognito import _lock
    except Exception:
        _warning("state.db mutation skipped: privacy admission unavailable")
        yield False
        return
    with _lock:
        yield not private_write(session_ids)


def private_write(session_ids: Any = ()) -> bool:
    """Use both turn scope and the registry so delayed callbacks remain private.

    A broken privacy lookup must drop the write, not expose the callback's SQL,
    exception, arguments or session ID to logs. No write-then-delete is safe:
    rolled-back content can already exist in WAL or freed SQLite pages.
    """
    try:
        from agent.incognito import is_incognito

        blocked = is_incognito() or any(is_incognito(sid) for sid in _identifiers(session_ids))
    except Exception:
        _warning("state.db mutation skipped: privacy state unavailable")
        return True
    if blocked:
        _warning("state.db mutation skipped: incognito")
    return bool(blocked)
