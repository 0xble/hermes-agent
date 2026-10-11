"""Calling-session identity for MCP servers that opt in with ``caller_identity: true``.

One long-lived MCP server can serve every session in a gateway, and its process inherits the
gateway's ``os.environ``, whose ``HERMES_SESSION_ID`` is last-writer-wins across sessions. The only
trustworthy per-call identity is the session bound to the calling turn, so Hermes sends it as
request ``_meta["hermes/caller"]``. The model controls ``arguments`` only; an argument named
``_meta`` stays an argument.

Capture happens in the calling thread, before the call is scheduled, so the identity never
depends on which context the MCP loop runs the coroutine in.
"""

import logging
import sqlite3
import threading
import time
from typing import Optional

logger = logging.getLogger("tools.mcp_tool")

CALLER_META_KEY = "hermes/caller"
_SUBAGENT_SOURCE = "subagent"
_DB_TIMEOUT_SECONDS = 2.0
_LINEAGE_READ_LOCK = threading.Lock()


def _topic_session_id(session_id: str, db_path) -> Optional[str]:
    """Bound the entire ancestry read, including filesystem/SQLite stalls.

    A timed-out reader retains the lock until it actually exits. Later callers
    cannot accumulate abandoned workers behind the same blocked database.
    """
    deadline = time.monotonic() + _DB_TIMEOUT_SECONDS
    if not _LINEAGE_READ_LOCK.acquire(timeout=_DB_TIMEOUT_SECONDS):
        return None
    result: list[Optional[str]] = []

    def read() -> None:
        try:
            result.append(_read_topic_session_id(session_id, db_path, deadline))
        finally:
            _LINEAGE_READ_LOCK.release()

    worker = threading.Thread(target=read, name="mcp-caller-lineage", daemon=True)
    try:
        worker.start()
    except RuntimeError:
        _LINEAGE_READ_LOCK.release()
        raise
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        logger.warning("MCP caller identity: lineage read timed out for %s after %.1fs",
                       session_id, _DB_TIMEOUT_SECONDS)
        return None
    return result[0] if result else None


def _read_topic_session_id(session_id: str, db_path, deadline: float) -> Optional[str]:
    """Nearest session in *session_id*'s parent chain whose source is not ``subagent`` (the session
    itself when it is not one), read-only. A delegated child has no topic of its own; topic sessions
    have parents too (compression, session switches), so the walk stops at the first non-subagent
    rather than climbing to an old root. None when a row is missing, the chain cycles, or the
    database cannot be read."""
    try:
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True,
                               timeout=max(0.0, deadline - time.monotonic()))
    except (sqlite3.Error, OSError):
        return None
    try:
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        current, seen = session_id, set()
        while current not in seen:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            conn.execute(f"PRAGMA busy_timeout = {int(remaining * 1000)}")
            seen.add(current)
            row = conn.execute("SELECT parent_session_id, source FROM sessions WHERE id = ?", (current,)).fetchone()
            if row is None:
                return None
            parent, source = row
            if source != _SUBAGENT_SOURCE:
                return current
            if not parent:
                return None
            current = parent
        return None  # cycle
    except sqlite3.Error:
        logger.debug("MCP caller identity: session lineage read failed for %s", session_id, exc_info=True)
        return None
    finally:
        conn.close()


def _profile_name() -> str:
    from gateway.session_context import bound_session_env
    if profile := bound_session_env("HERMES_SESSION_PROFILE"):
        return profile
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name()
    except Exception:
        return ""


def caller_identity_meta() -> Optional[dict]:
    """Request ``_meta`` naming the calling session, or None when no session is bound (never
    invented). Call in the calling thread, before scheduling on the MCP loop."""
    from gateway.session_context import bound_session_env
    session_id = bound_session_env("HERMES_SESSION_ID")
    if not session_id:
        return None
    from agent.delegation_context import is_delegated_child_process_context
    source = bound_session_env("HERMES_SESSION_SOURCE")
    is_child = is_delegated_child_process_context() or source == _SUBAGENT_SOURCE
    # Execution context is authoritative even when persistence never opened.
    # Children inherit their parent's cron/source vars, so check delegation first.
    top_level = not is_child and (source or bound_session_env("HERMES_CRON_SESSION") == "1")
    from hermes_constants import get_hermes_home
    return {CALLER_META_KEY: {
        "profile": _profile_name() or None,
        "session_id": session_id,
        "topic_session_id": session_id if top_level else _topic_session_id(session_id, get_hermes_home() / "state.db"),
    }}
