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
from typing import Optional

logger = logging.getLogger("tools.mcp_tool")

CALLER_META_KEY = "hermes/caller"
_SUBAGENT_SOURCE = "subagent"
_DB_TIMEOUT_SECONDS = 2.0


def _topic_session_id(session_id: str, db_path) -> Optional[str]:
    """Nearest session in *session_id*'s parent chain whose source is not ``subagent`` (the session
    itself when it is not one), read-only. A delegated child has no topic of its own; topic sessions
    have parents too (compression, session switches), so the walk stops at the first non-subagent
    rather than climbing to an old root. None when a row is missing, the chain cycles, or the
    database cannot be read."""
    try:
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=_DB_TIMEOUT_SECONDS)
    except (sqlite3.Error, OSError):
        return None
    try:
        current, seen = session_id, set()
        while current not in seen:
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
    from hermes_constants import get_hermes_home
    return {CALLER_META_KEY: {
        "profile": _profile_name() or None,
        "session_id": session_id,
        "topic_session_id": _topic_session_id(session_id, get_hermes_home() / "state.db"),
    }}
