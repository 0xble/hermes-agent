"""Loop revisions and settlement share the goal lifecycle's snapshot/CAS contract.

No database lock is held during authority lookup or judging. Mutation tokens detect
ABA changes; tick identity lets cleanup release only the claim that actually finished.
"""

from __future__ import annotations

import copy
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from hermes_cli.lifecycle_snapshot import commit_and_publish_snapshot, snapshot_control

if TYPE_CHECKING:
    from hermes_cli.loops import LoopState

logger = logging.getLogger(__name__)


class LoopSnapshotChanged(RuntimeError):
    """The durable loop changed while a revision or judge was in flight."""


class LoopPersistenceError(RuntimeError):
    """The loop snapshot could not be read or committed."""


def _read_loop(session_id: str, db: Any) -> LoopState | None:
    from hermes_cli.loops import LoopState, _meta_key
    try:
        raw = db.get_meta(_meta_key(session_id))
        return LoopState.from_json(raw) if raw else None
    except Exception as exc:
        raise LoopPersistenceError from exc


def assert_loop_snapshot(session_id: str, expected: str, db: Any) -> None:
    current = _read_loop(session_id, db)
    if current is None or current.to_json() != expected:
        raise LoopSnapshotChanged("loop state changed")


def _commit(session_id: str, expected: str, state: LoopState, db: Any) -> None:
    from hermes_cli.loops import LoopState, _meta_key

    def write(cursor):
        row = cursor.execute(
            "SELECT value FROM state_meta WHERE key = ?", (_meta_key(session_id),)
        ).fetchone()
        if not row or not row[0] or LoopState.from_json(row[0]).to_json() != expected:
            raise LoopSnapshotChanged("loop state changed")
        if state.to_json() != expected:
            state.mutation_id = uuid.uuid4().hex
            db.set_meta(_meta_key(session_id), state.to_json(), cursor=cursor)
    try:
        db._execute_write(write)
    except LoopSnapshotChanged:
        raise
    except Exception as exc:
        raise LoopPersistenceError from exc


def _same_tick(first: LoopState, second: LoopState) -> bool:
    if not first.awaiting_response or not second.awaiting_response:
        return False
    if first.tick_id or second.tick_id:
        return bool(first.tick_id) and first.tick_id == second.tick_id
    # Compatibility for an already-running tick written before claim IDs existed.
    return (first.created_at, first.last_fired_at, first.ticks_fired) == (
        second.created_at, second.last_fired_at, second.ticks_fired,
    )


@snapshot_control
def _release_superseded_tick(manager: Any, original: LoopState, db: Any) -> None:
    """Release only the SAME tick on the winner, never apply its obsolete verdict.

    A revision may preserve awaiting_response. Leaving that claim behind would
    wedge the loop. Pauses, clears, replacements and newer ticks are untouched.
    Cleanup uses another CAS so it cannot overwrite a second concurrent edit.
    """
    from hermes_cli.loops import LoopState
    winner = _read_loop(manager.session_id, db)
    manager._state = winner
    if winner is None or winner.status != "active" or not _same_tick(original, winner):
        return
    expected = winner.to_json()
    updated = LoopState.from_json(expected)
    updated.awaiting_response = False
    updated.next_due_at = time.time() + updated.current_delay
    try:
        commit_and_publish_snapshot(
            manager, updated, lambda: _commit(manager.session_id, expected, updated, db),
            lambda: _read_loop(manager.session_id, db), preserve_reference=False,
        )
    except LoopSnapshotChanged:
        manager._state = _read_loop(manager.session_id, db)


@snapshot_control
def _reload_if_readable(manager: Any, db: Any) -> None:
    if db is not None:
        try:
            manager._state = _read_loop(manager.session_id, db)
        except LoopPersistenceError:
            logger.warning("Could not reload loop %s; retaining in-memory state", manager.session_id)


def _failure(manager: Any, settlement: bool, code: str, message: str) -> dict[str, Any]:
    if settlement:
        return {"stopped": False, "status": manager.state.status if manager.state else None,
                "error_code": code, "reason": message,
                "message": message if code == "persistence_failed" else ""}
    return manager._revision_result(
        ok=False, revision=None, version=manager.state.version if manager.state else 0,
        error_code=code, error=message,
    )


def mutate_loop_snapshot(manager: Any, method: str, *args, **kwargs) -> dict[str, Any]:
    """Stage a revision or settlement without persisting intermediate states."""
    from hermes_cli.loops import LoopState, _get_session_db
    settlement = method == "_complete_tick"
    original = manager.state
    if not manager.session_id or original is None:
        # No session means intentionally ephemeral state, not a failed durable write.
        return getattr(manager, method)(*args, **kwargs)
    expected = original.to_json()
    # Commands on this same manager can mutate original, including its claim ID.
    initial = LoopState.from_json(expected)
    db = None
    try:
        try:
            db = _get_session_db()
        except Exception as exc:
            raise LoopPersistenceError from exc
        if db is None:
            raise LoopPersistenceError("session DB unavailable")
        assert_loop_snapshot(manager.session_id, expected, db)
        staged = copy.copy(manager)
        staged._state = LoopState.from_json(expected)
        staged._evaluation_snapshot = (db, expected)
        result = getattr(staged, method)(*args, **kwargs)
        # Even a no-op must validate at the transactional boundary.
        published = commit_and_publish_snapshot(
            manager, staged.state,
            lambda: _commit(manager.session_id, expected, staged.state, db),
            lambda: _read_loop(manager.session_id, db),
        )
        if settlement and not published:
            return _failure(manager, True, "state_changed", "loop changed after settlement committed")
        return result
    except LoopSnapshotChanged:
        try:
            if settlement:
                _release_superseded_tick(manager, initial, db)
            else:
                with manager._snapshot_lock:
                    manager._state = _read_loop(manager.session_id, db)
        except LoopPersistenceError:
            logger.warning("Could not reload or release superseded loop %s", manager.session_id, exc_info=True)
            return _failure(manager, settlement, "persistence_failed",
                            "Loop state could not be read or saved; settlement stopped.")
        return _failure(manager, settlement, "state_changed", "loop changed; retry against the current version")
    except LoopPersistenceError:
        logger.warning("Loop mutation could not read or commit durable state for %s", manager.session_id, exc_info=True)
        _reload_if_readable(manager, db)
        return _failure(manager, settlement, "persistence_failed",
                        "Loop state could not be read or saved; mutation was not applied.")
