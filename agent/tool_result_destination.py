"""Invocation-owned archive destinations, retained through worker and batch boundaries."""
from contextlib import contextmanager
from contextvars import ContextVar


_destinations = ContextVar("tool_batch_destinations", default=None)


@contextmanager
def result_destination_batch():
    # Segmented execution shares its outer batch with nested sequential/parallel calls.
    existing = _destinations.get()
    token = _destinations.set({}) if existing is None else None
    try:
        yield
    finally:
        if token is not None:
            _destinations.reset(token)


def remember_result_environment(message, env):
    destinations = _destinations.get()
    if destinations is not None:
        destinations[id(message)] = env


def result_environment(message, fallback):
    destinations = _destinations.get()
    return destinations.get(id(message), fallback) if destinations is not None else fallback
