"""Wait-window clock, default and model-facing guidance for ``process_manage(action='wait')``."""

import time
from typing import Tuple

# Only a process whose completion notice will actually reach this session benefits
# from a short wait: the session can end the turn and be woken. Every other caller
# can only wait, so it keeps the full configured window.
NOTIFIABLE_DEFAULT_TIMEOUT = 30


def monotonic() -> float:
    """Wait-window clock; a module seam so tests never patch the shared time module."""
    return time.monotonic()


def can_receive_later() -> bool:
    """Whether a completion notice or later turn can still reach this session.

    False for stateless channels (cron, one-shot CLI, Kanban workers, stateless
    HTTP) and for delegated children, whose notices reach nobody.
    """
    from agent.delegation_context import is_delegated_child_context
    from gateway.session_context import async_delivery_supported

    return async_delivery_supported() and not is_delegated_child_context()


def completion_reaches_session(session) -> bool:
    """Whether the registry's completion drain would route this process to this session.

    Processes owned by a delegated ``sa-`` task are suppressed by the drain (the child
    result is the deliverable) and are killed at child teardown unless explicitly handed
    off, so they never get the short, end-the-turn plan in their parent. The spawning
    session id is checked first because the gateway drops a notice across a user
    boundary such as /new, which keeps the chat's session key.
    """
    if str(session.owner_task_id or session.task_id or "").startswith("sa-"):
        return False
    from gateway.session_context import get_session_env

    current_session_id = str(get_session_env("HERMES_SESSION_ID", "") or "")
    current_session_key = str(get_session_env("HERMES_SESSION_KEY", "") or "")
    parent_session_id = str(session.parent_session_id or "")
    session_key = str(session.session_key or "")
    if parent_session_id and current_session_id:
        return parent_session_id == current_session_id
    if session_key and current_session_key:
        return session_key == current_session_key
    # Legacy/CLI callers may have no session routing metadata at all. Preserve
    # their existing local completion behavior, while failing closed whenever a
    # process does carry an origin that is not the current session.
    return not parent_session_id and not session_key


def plan_for(session, max_timeout: int) -> Tuple[int, str]:
    """``wait_plan`` for a registry process session."""
    return wait_plan(bool(session.notify_on_complete), max_timeout,
                     completion_reaches_session=completion_reaches_session(session))


def wait_plan(
    notify_on_complete: bool,
    max_timeout: int,
    *,
    completion_reaches_session: bool = True,
) -> Tuple[int, str]:
    """The single decision for a wait: (default window, guidance after it elapses).

    Deferring is safe only when a completion notice will actually arrive: the
    process has notify_on_complete set, its completion is routed to this session,
    AND this session can receive it later.
    Anywhere else, ending the turn or not waiting again loses the result, so the
    caller keeps the full window and must collect or kill the process itself.
    """
    reachable = can_receive_later()
    if notify_on_complete and completion_reaches_session and reachable:
        return (min(NOTIFIABLE_DEFAULT_TIMEOUT, max_timeout),
                " You will be notified on completion; do not wait again. End the turn or do other work.")
    from agent.delegation_context import is_delegated_child_context

    if is_delegated_child_context():
        return (max_timeout,
                " You are a subagent: no completion notice will reach you or your parent, and this process is "
                "killed when you finish. Before finishing, wait again with a bounded timeout, kill it, or hand "
                "it to your parent with process_manage(action='handoff', session_id=..., data='<purpose>').")
    if not completion_reaches_session:
        reason = "This process's completion will not be routed to this session."
    elif not notify_on_complete:
        reason = "Completion notification was not enabled for this process, so no notice will arrive."
    elif not reachable:
        reason = "This session cannot receive a later completion notice or turn."
    else:
        reason = "The completion notice is not available to this session."
    return (max_timeout, f" {reason} Before finishing, wait again with a bounded timeout or kill it.")
