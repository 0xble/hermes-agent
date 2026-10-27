"""Attempt-local write-lock diagnostics, without SQL tracing or extra database reads."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import time


@dataclass
class WriteMetrics:
    phases: dict[str, float] = field(default_factory=dict)
    rows: dict[str, int] = field(default_factory=dict)

    def add_time(self, name: str, elapsed: float) -> None:
        self.phases[name] = self.phases.get(name, 0.0) + elapsed

    def format(self) -> str:
        phases = ",".join(f"{name}={elapsed:.3f}s" for name, elapsed in self.phases.items())
        rows = ",".join(f"{name}={count}" for name, count in self.rows.items()) or "none"
        # SQLite executes the FTS triggers inside the INSERT/UPDATE statements. Separating their
        # runtime would require SQL tracing or changing the triggers on the hot writer path.
        return f"phases[{phases}] rows[{rows}] fts=inline"


current_write_metrics: ContextVar[WriteMetrics | None] = ContextVar("state_write_metrics", default=None)


def add_write_rows(name: str, count: int) -> None:
    metrics = current_write_metrics.get()
    if metrics is not None:
        metrics.rows[name] = metrics.rows.get(name, 0) + count


@contextmanager
def write_phase(name: str, *, thread_cpu: bool = False):
    metrics = current_write_metrics.get()
    if metrics is None:
        yield
        return
    started = time.monotonic()
    cpu_started = time.thread_time() if thread_cpu else None
    try:
        yield
    finally:
        cpu_elapsed = time.thread_time() - cpu_started if cpu_started is not None else None
        metrics.add_time(name, time.monotonic() - started)
        if cpu_elapsed is not None:
            metrics.add_time(f"{name}_cpu", cpu_elapsed)


def timed_write_phase(name: str):
    def decorate(fn):
        @wraps(fn)
        def measured(*args, **kwargs):
            with write_phase(name):
                return fn(*args, **kwargs)
        return measured
    return decorate
