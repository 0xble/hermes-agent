"""Ticker-owned ledger handles keep WAL readers available between short transactions."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
import sqlite3


class ExecutionStoreOwners(AbstractContextManager):
    """One idle connection per served home, closed on departure or ticker shutdown.

    Work connections must still close deterministically. Without an explicit owner,
    their last close checkpoints and removes WAL/SHM, making read-only SQLite
    builds unable to open the ledger between ticks. No transaction is held here.
    """

    def __init__(self):
        self._connections: dict[Path, sqlite3.Connection] = {}

    def retain(self, home: Path | str, *, gate=None, profile_name=None) -> None:
        # The ticker has already bound this home's profile scope. Use the same
        # initializer and WAL fallback as ordinary ledger work, not a second stack.
        from cron.executions import _connect

        if gate is not None and not gate(profile_name, home):
            return
        key = Path(home).resolve()
        if key not in self._connections:
            conn = _connect()
            try:
                conn.commit()
                # WAL mode alone need not materialize SHM; finish a real read.
                conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
            except BaseException:
                conn.close()
                raise
            self._connections[key] = conn

    def run(self, home, action, **kwargs):
        """Attempt ownership before work; report failure after degraded-store cleanup."""
        error = None
        try:
            self.retain(home)
        except BaseException as exc:
            error = exc
        result = action(**kwargs)
        if error is not None:
            raise error
        return result

    def discard_except(self, homes) -> None:
        current = {Path(home).resolve() for home in homes}
        for home in self._connections.keys() - current:
            self._connections.pop(home).close()

    def __exit__(self, exc_type, exc_value, traceback):
        self.discard_except(())
        return False
