"""Publish committed goal/loop snapshots without undoing lifecycle commands."""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable


def snapshot_control(method: Callable) -> Callable:
    """Serialize short controls with snapshot publication, not with model judging."""
    @wraps(method)
    def controlled(manager, *args, **kwargs):
        with manager._snapshot_lock:
            return method(manager, *args, **kwargs)
    return controlled


def commit_and_publish_snapshot(
    manager: Any, staged: Any, commit: Callable[[], None], read: Callable[[], Any],
    *, preserve_reference: bool = True,
) -> bool:
    """Commit and adopt only the durable winner under the manager's control lock.

    The transaction can release its DB lock before returning. A newer writer
    must therefore win even after a successful CAS. Reentrant controls can also
    run inside that boundary; never publish over their changed in-memory state.
    Return whether the staged snapshot still won, so evaluators can suppress a
    superseded continuation or settlement message as well as stale state.

    Stored-row commands derive their staged row inside the write transaction: they pass
    ``staged=None`` and ``commit`` returns the row it wrote (or validated unchanged).
    """
    with manager._snapshot_lock:
        committed = commit()
        if staged is None:
            staged = committed
        original = manager.state
        expected_memory = original.to_json() if original is not None else None
        winner = read()
        if manager.state is not original or (
            original is not None and original.to_json() != expected_memory
        ):
            manager._state = winner
            return False
        if winner is not None and staged is not None and winner.to_json() == staged.to_json():
            if preserve_reference and original is not None:
                original.__dict__.update(staged.__dict__)
                manager._state = original
            else:
                manager._state = staged
            return True
        manager._state = winner
        return False
