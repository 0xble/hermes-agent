"""Runtime-only goal authority, separate from decorated model input.

Gateway callers bind authenticated message bodies before adding reply/context
wrappers. None means a local caller may use its current user message. An empty
body means an internal gateway turn has no fresh user authorization. Never
populate this context from model arguments or parsed prompt markers.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_goal_user_request: ContextVar[tuple[str, str] | None] = ContextVar(
    "goal_user_request", default=None,
)


@contextmanager
def goal_user_request_scope(session_id: str, text: str) -> Iterator[None]:
    token = _goal_user_request.set((session_id, text))
    try:
        yield
    finally:
        _goal_user_request.reset(token)


def goal_authorization_task(session_id: str, fallback: str | None) -> str | None:
    request = _goal_user_request.get()
    if request is None:
        return fallback
    owner, text = request
    return text if owner == session_id else ""
