"""Loop revisions and settlement share the goal lifecycle's snapshot/CAS contract.

No database lock is held during authority lookup or judging. Mutation tokens detect
ABA changes; tick identity lets cleanup release only the claim that actually finished.
Commits go through ``save_loop``'s stored-row transaction, so the loop control fence
advances exactly as it does for every other loop write, including inside a cursor owner's
transaction.
"""

from __future__ import annotations

import copy
import logging
import time
from typing import TYPE_CHECKING, Any, Optional, Tuple

from hermes_cli.lifecycle_snapshot import commit_and_publish_snapshot, snapshot_control

if TYPE_CHECKING:
    from hermes_cli.loops import LoopState

logger = logging.getLogger(__name__)


class LoopSnapshotChanged(RuntimeError):
    """The durable loop changed while a revision or judge was in flight."""


class LoopPersistenceError(RuntimeError):
    """The loop snapshot could not be read or committed."""


def _read_loop(session_id: str, db: Any, cursor: Any = None) -> LoopState | None:
    from hermes_cli.loops import LoopState, _meta_key
    try:
        if cursor is not None:
            # A cursor owner holds the write lock; a separate SessionDB read would deadlock.
            row = cursor.execute(
                "SELECT value FROM state_meta WHERE key = ?", (_meta_key(session_id),)
            ).fetchone()
            raw = row[0] if row else None
        else:
            raw = db.get_meta(_meta_key(session_id))
        return LoopState.from_json(raw) if raw else None
    except Exception as exc:
        if cursor is not None:
            raise  # transaction owners see their own failures
        raise LoopPersistenceError from exc


def assert_loop_snapshot(session_id: str, expected: str, db: Any, *, cursor: Any = None) -> None:
    current = _read_loop(session_id, db, cursor)
    if current is None or current.to_json() != expected:
        raise LoopSnapshotChanged("loop state changed")


def _commit(manager: Any, expected: str, state: LoopState, *, tick_lifecycle: bool) -> LoopState:
    """Write *state* only while the stored row is still *expected*; return the row written."""
    from hermes_cli.loops import _overlay, save_loop

    def apply(live: Optional[LoopState]) -> bool:
        if live is None or live.to_json() != expected:
            return False
        _overlay(live, state)  # a no-op stays unwritten, validated inside the same transaction
        return True

    try:
        saved = save_loop(manager.session_id, cursor=manager._cursor, apply=apply,
                          tick_lifecycle=tick_lifecycle)
    except Exception as exc:
        if manager._cursor is not None:
            raise  # transaction owners see their own failures
        raise LoopPersistenceError from exc
    if saved is None:
        raise LoopPersistenceError("loop write failed")
    if not saved[1]:
        raise LoopSnapshotChanged("loop state changed")
    return saved[0]


def _mark_synced(manager: Any) -> None:
    """Record the published row's schedule; tick claims treat any other value as a reschedule."""
    mark = getattr(manager, "_mark_synced", None)
    if callable(mark):
        mark()


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
def _release_superseded_tick(manager: Any, original: LoopState, db: Any) -> Optional[Tuple[str, str, str]]:
    """Release only the SAME tick on the winner, never apply its obsolete verdict.

    A revision may preserve awaiting_response. Leaving that claim behind would
    wedge the loop. Pauses, clears, replacements and newer ticks are untouched.
    Cleanup uses another CAS so it cannot overwrite a second concurrent edit.
    """
    from hermes_cli.loops import LoopState, self_paced_floor_seconds
    winner = _read_loop(manager.session_id, db, manager._cursor)
    manager._state = winner
    _mark_synced(manager)
    if winner is None or winner.status != "active" or not _same_tick(original, winner):
        return
    expected = winner.to_json()
    updated = LoopState.from_json(expected)
    updated.awaiting_response = False
    updated.current_delay = updated.interval_seconds if updated.mode == "interval" else updated.current_delay
    cap = manager._cap_stop(updated)
    if cap is None:
        updated.next_due_at = time.time() + (updated.current_delay or updated.interval_seconds or self_paced_floor_seconds())
    else:
        manager._apply_stop(updated, cap[0], cap[1])
    try:
        commit_and_publish_snapshot(
            manager, None, lambda: _commit(manager, expected, updated, tick_lifecycle=True),
            lambda: _read_loop(manager.session_id, db, manager._cursor), preserve_reference=False,
        )
        _mark_synced(manager)
        return cap
    except LoopSnapshotChanged:
        manager._state = _read_loop(manager.session_id, db, manager._cursor)
        _mark_synced(manager)
        return None


@snapshot_control
def _reload_if_readable(manager: Any, db: Any) -> None:
    if db is not None:
        try:
            manager._state = _read_loop(manager.session_id, db, manager._cursor)
            _mark_synced(manager)
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
    cursor = getattr(manager, "_cursor", None)
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
        # A cursor owner's snapshot read and CAS run through its own transaction, never a
        # second SessionDB lock acquisition that would deadlock against it.
        assert_loop_snapshot(manager.session_id, expected, db, cursor=cursor)
        staged = copy.copy(manager)
        staged._state = LoopState.from_json(expected)
        staged._evaluation_snapshot = (db, expected)
        result = getattr(staged, method)(*args, **kwargs)
        # Even a no-op must validate at the transactional boundary.
        published = commit_and_publish_snapshot(
            manager, None,
            lambda: _commit(manager, expected, staged.state, tick_lifecycle=settlement),
            lambda: _read_loop(manager.session_id, db, cursor),
        )
        _mark_synced(manager)
        if settlement and not published:
            return _failure(manager, True, "state_changed", "loop changed after settlement committed")
        return result
    except LoopSnapshotChanged:
        try:
            if settlement and manager._follow_compression():
                # Compression may move the claimed row after this manager read it. Retry the
                # complete staged mutation against the validated successor's snapshot only while
                # it still holds this exact claim; otherwise release below as superseded.
                with manager._snapshot_lock:
                    moved = _read_loop(manager.session_id, db)
                    follow = moved is not None and _same_tick(initial, moved)
                    if follow:
                        manager._state = moved
                        _mark_synced(manager)
                if follow:  # re-judge outside the lock: controls never wait on judging
                    return mutate_loop_snapshot(manager, method, *args, **kwargs)
            if settlement:
                # The winner's caps still apply to the released claim, but the obsolete verdict is
                # never published: callers see state_changed with no user-visible message.
                released = _release_superseded_tick(manager, initial, db)
                if released is not None:
                    return {"status": released[0], "stopped": True, "reason": released[1],
                            "message": "", "error_code": "state_changed"}
            else:
                with manager._snapshot_lock:
                    manager._state = _read_loop(manager.session_id, db, cursor)
                    _mark_synced(manager)
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
