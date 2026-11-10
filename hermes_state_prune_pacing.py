"""One retention prune pass per store, paced so foreground writers keep getting the write lock.

On 2026-11-07 a CLI prune whose client timed out was started again while the gateway, which
shields the work, was still running the first pass. The two passes alternated 25-session
transactions of 5-20 s each for five hours with a 10 ms gap between them, so every foreground
writer waited behind them. A second pass on the same store is now refused, and each pass sizes
its transactions by measured hold time and yields at least as long as it held the lock.
"""
import os
from contextlib import contextmanager
import threading
import time

_PASSES: dict[str, threading.Lock] = {}
_PASSES_GUARD = threading.Lock()


class PruneInProgressError(RuntimeError):
    """Another prune pass on this store is still running in this process."""


class DeletionWindowClosedError(RuntimeError):
    """A window-bound prune was asked to start while ``maintenance.deletion_window`` is closed."""


@contextmanager
def prune_pass(db_path):
    """Hold this process's single prune slot for the store at *db_path*, or refuse."""
    key = os.path.realpath(str(db_path))
    with _PASSES_GUARD:
        lock = _PASSES.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        raise PruneInProgressError(
            "A session prune is already running on this store. It keeps running after a client "
            "timeout or disconnect; wait for it to finish before starting another.")
    try:
        yield
    finally:
        lock.release()


class PrunePacer:
    """Adaptive batch size plus a writer-fairness pause after every prune transaction.

    A batch that holds the lock longer than ``TARGET_S`` halves the next one (down to a single
    session); a quick one doubles it back up to ``max_size``. The pause matches the time the
    lock was held (0.05-2 s), so up to a 2 s hold a prune holds the writer about half the time.
    """
    TARGET_S = 0.5
    MIN_PAUSE_S = 0.05
    MAX_PAUSE_S = 2.0

    def __init__(self, max_size=25):
        self.max_size = self.size = max_size

    def pause(self, held_s):
        if held_s > self.TARGET_S:
            self.size = max(1, self.size // 2)
        elif held_s < self.TARGET_S / 4:
            self.size = min(self.max_size, self.size * 2)
        from hermes_state_prune_budget import current_budget
        seconds = min(max(held_s, self.MIN_PAUSE_S), self.MAX_PAUSE_S)
        if budget := current_budget():
            budget.pause(seconds)
        else:
            time.sleep(seconds)
