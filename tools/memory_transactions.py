"""Optional profile-scoped observers around a native locked memory commit.

Observers are context managers: prepare before yielding, commit after yielding.
Errors propagate. An observer that promises durable undo must refuse a write when
it cannot prepare and fence incomplete commits itself. Snapshots never enter the
model-facing memory result. Register once during plugin loading, not per tool call.
"""
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Callable, ContextManager
from uuid import uuid4


@dataclass(frozen=True)
class MemoryTransaction:
    target: str
    path: Path
    before: str
    after: str
    metadata: dict[str, Any] = field(default_factory=dict)
    transaction_id: str = field(default_factory=lambda: uuid4().hex)


_LOCK = RLock()
_OBSERVERS: dict[tuple[str, str], tuple[Callable[[MemoryTransaction], ContextManager], bool]] = {}


def _profile() -> str:
    from hermes_constants import get_hermes_home
    return str(get_hermes_home().resolve())


def register_memory_transaction_observer(
    name: str, observer: Callable, *, durable: bool = False,
) -> Callable[[], None]:
    """Register for this profile; durable observers must prepare undo or refuse the write."""
    key = (_profile(), name)
    registration = (observer, durable)
    with _LOCK:
        _OBSERVERS[key] = registration

    def unregister():
        with _LOCK:
            if _OBSERVERS.get(key) is registration:
                _OBSERVERS.pop(key)
    return unregister


def has_durable_memory_transaction_observer() -> bool:
    """Whether the current profile has an observer promising durable undo."""
    profile = _profile()
    with _LOCK:
        return any(durable for (home, _), (_, durable) in _OBSERVERS.items() if home == profile)


class DurableMemoryObserverUnavailable(RuntimeError):
    """A write that requires durable undo found no durable observer; nothing was written."""


# Admission and commit are separate steps, so an observer can unregister in between. A
# write admitted only because a durable journal existed sets this; the commit then checks
# the snapshot it actually uses rather than trusting the earlier admission check.
_REQUIRE_DURABLE: ContextVar[bool] = ContextVar("memory_require_durable_observer", default=False)


@contextmanager
def require_durable_memory_observer():
    """Writes inside raise DurableMemoryObserverUnavailable unless their observers include a durable one."""
    token = _REQUIRE_DURABLE.set(True)
    try:
        yield
    finally:
        _REQUIRE_DURABLE.reset(token)


@contextmanager
def observe_memory_transaction(transaction: MemoryTransaction):
    """Caller holds the target file lock throughout enter, write and exit."""
    profile = _profile()
    with _LOCK:
        registrations = [entry for (home, _), entry in _OBSERVERS.items() if home == profile]
    if _REQUIRE_DURABLE.get() and not any(durable for _, durable in registrations):
        raise DurableMemoryObserverUnavailable("No durable memory journal is registered for this profile.")
    observers = [callback for callback, _ in registrations]
    with ExitStack() as stack:
        for callback in observers:
            stack.enter_context(callback(transaction))
        yield
