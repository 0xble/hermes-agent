"""Context-local deadlines for bounded gateway handover operations."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import Context, ContextVar, copy_context
import sqlite3
import time
import inspect
from functools import wraps
from typing import Any, Callable, Iterator


_current_deadline: ContextVar[float | None] = ContextVar("gateway_deadline", default=None)


def now() -> float:
    """Return the canonical monotonic clock used by bounded gateway operations."""
    return time.monotonic()


def detached_context() -> Context:
    """Return a copy of the caller's context with no gateway deadline.

    Long-lived tasks (drain, heartbeat, pollers, recovery) must not inherit a
    request's deadline, but they must keep every other context variable, such
    as the Telegram polling generation that gates journaled wire commits.
    """
    ctx = copy_context()
    ctx.run(_current_deadline.set, None)
    return ctx


def current() -> float | None:
    """Return the active absolute monotonic deadline, if any."""
    return _current_deadline.get()


def remaining() -> float | None:
    """Return seconds remaining in the active scope, or ``None`` if unbounded."""
    deadline = current()
    if deadline is None:
        return None
    return max(0.0, deadline - now())


def check() -> None:
    """Raise ``TimeoutError`` when the active scope has expired."""
    budget = remaining()
    if budget is not None and budget <= 0:
        raise TimeoutError("gateway deadline exceeded")


def connect_sqlite(path: Any, *, timeout: float = 5.0, **kwargs: Any) -> sqlite3.Connection:
    """Open SQLite with the ambient deadline folded into its busy timeout."""
    budget = remaining()
    if budget is not None:
        if budget <= 0:
            raise TimeoutError("gateway deadline exceeded")
        timeout = min(float(timeout), budget)
    conn = sqlite3.connect(path, timeout=max(0.0, timeout), **kwargs)
    if budget is not None:
        conn.execute(f"PRAGMA busy_timeout={max(0, int(timeout * 1000))}")
    return conn


def begin_immediate(conn: sqlite3.Connection) -> None:
    """Acquire the coordinator write lock and re-check the ambient deadline."""
    check()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if remaining() == 0:
            conn.rollback()
            raise TimeoutError("gateway deadline exceeded") from exc
        raise
    try:
        check()
    except TimeoutError:
        conn.rollback()
        raise


def with_deadline_scope(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Adapt legacy explicit ``deadline=`` parameters to the ambient scope."""
    if inspect.iscoroutinefunction(fn):
        @wraps(fn)
        async def async_wrapped(*args: Any, **kwargs: Any) -> Any:
            deadline = kwargs.get("deadline")
            with deadline_scope(deadline):
                return await fn(*args, **kwargs)
        return async_wrapped
    @wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        deadline = kwargs.get("deadline")
        with deadline_scope(deadline):
            return fn(*args, **kwargs)
    return wrapped


@contextmanager
def deadline_scope(deadline: float | None, *, inherit: bool = True) -> Iterator[float | None]:
    """Install a deadline, retaining the earlier deadline in nested scopes by default.

    Deadlines are absolute ``time.monotonic()`` values. ``None`` leaves an
    existing scope unchanged and does not create an unbounded inner window.
    Recovery paths may pass ``inherit=False`` when their reserved budget starts
    after the caller's cooperative window has ended.
    """
    parent = current()
    if deadline is None:
        effective = parent
    elif not inherit or parent is None:
        effective = float(deadline)
    else:
        effective = min(parent, float(deadline))
    token = _current_deadline.set(effective)
    try:
        yield effective
    finally:
        _current_deadline.reset(token)


@contextmanager
def unbounded_scope() -> Iterator[None]:
    """Temporarily clear an ambient deadline for a separately reserved recovery path."""
    token = _current_deadline.set(None)
    try:
        yield None
    finally:
        _current_deadline.reset(token)


__all__ = ["begin_immediate", "check", "connect_sqlite", "current", "deadline_scope", "detached_context", "now", "remaining", "unbounded_scope", "with_deadline_scope"]
