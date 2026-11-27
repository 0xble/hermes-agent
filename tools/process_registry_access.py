"""Which caller may act on a ``process_manage`` session (poll/log/wait/kill/write/submit/close).

A session id is a routing handle, not a capability (SECURITY.md §2.6): another conversation that
learns a live id must not read, kill, or type into that process. The rule mirrors ``list_sessions``
and the retained-result loader so a process the caller can list is one it can act on.
"""

from __future__ import annotations

from contextlib import suppress


def caller_session_key() -> str:
    """The current gateway/CLI session key, or "" outside one (#29177)."""
    with suppress(Exception):
        from tools.approval_context import get_current_session_key
        return get_current_session_key(default="") or ""
    return ""


def session_action_allowed(session, task_id: str, session_key: str) -> bool:
    """Whether the caller may poll/log/wait/kill/write/submit/close ``session``.

    Ownership is the raw spawning task (``owner_task_id``), not the potentially
    shared container ``task_id``, or the gateway ``session_key`` — a
    session-scoped process started by a sibling task is still this
    conversation's to manage (#29177), including through a compression resume.
    A caller carrying no identity at all is trusted internal plumbing rather
    than a model call (model calls always pass task_id); a session carrying no
    identity (unowned/detached) has no owner to violate.
    """
    owner = session.owner_task_id or session.task_id
    if getattr(session, "ssh_binding", None) is not None:
        # Named-node handles are profile-scoped capabilities: the binding carries the
        # original node and profile, and the node-aware dispatcher validates that binding
        # before acting. Task/session lineage is not available for a recovered SSH handle
        # after the originating tool turn has ended.
        return True
    if task_id and task_id == owner:
        return True
    if session_key and session_key == session.session_key:
        return True
    from tools.process_registry_results import _owns_result
    # Compression continuations: the CLI and TUI rotate the session key, /v1/runs the task id
    # (its session key is a fresh run id per run), so either lineage ties the caller to its process.
    db_path = None
    binding = getattr(session, "ssh_binding", None)
    if binding is not None:
        from pathlib import Path
        db_path = Path(binding.profile_home) / "state.db"
    lineage = {}
    if db_path is not None:
        lineage["db_path"] = db_path
    if (task_id and _owns_result(task_id, owner, **lineage)) or (session_key and _owns_result(session_key, session.session_key, **lineage)):
        return True
    return not (task_id or session_key) or not (owner or session.session_key)


def resolve_actionable(session_id: str, task_id, session_key: str = "") -> tuple[bool, str]:
    """``(allowed, id_to_act_on)``. A session that exists but belongs to someone else is refused (the
    caller then answers with the same ``not_found`` shape as a miss, so foreign ids are not
    enumerable). An id that resolves to no session (missing, or an ambiguous prefix) is refused
    the same way, and an allowed session is acted on by its FULL id, so no prefix the check did
    not resolve can reach another conversation's process."""
    from tools.process_registry import process_registry
    # Ownership checks must not refresh a remote handle. A conflicting or foreign
    # request is refused before it can probe the process's original node.
    session = process_registry._lookup_session(session_id)
    if session is None:
        return False, session_id
    caller_key = session_key or caller_session_key()
    if not session_action_allowed(session, str(task_id or ""), caller_key):
        return False, session_id
    return True, session.id
