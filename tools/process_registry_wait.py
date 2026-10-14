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


def wait_plan(notify_on_complete: bool, max_timeout: int) -> Tuple[int, str]:
    """The single decision for a wait: (default window, guidance after it elapses).

    Deferring is safe only when a completion notice will actually arrive: the
    process has notify_on_complete set AND this session can receive it later.
    Anywhere else, ending the turn or not waiting again loses the result, so the
    caller keeps the full window and must collect or kill the process itself.
    """
    if notify_on_complete and can_receive_later():
        return (min(NOTIFIABLE_DEFAULT_TIMEOUT, max_timeout),
                " You will be notified on completion; do not wait again. End the turn or do other work.")
    from agent.delegation_context import is_delegated_child_context

    if is_delegated_child_context():
        return (max_timeout,
                " You are a subagent: no completion notice will reach you or your parent, and this process is "
                "killed when you finish. Before finishing, wait again with a bounded timeout, kill it, or hand "
                "it to your parent with process_manage(action='handoff', session_id=..., data='<purpose>').")
    reason = ("Completion notification was not enabled for this process, so no notice will arrive."
              if can_receive_later() else "This session cannot receive a later completion notice or turn.")
    return (max_timeout, f" {reason} Before finishing, wait again with a bounded timeout or kill it.")
