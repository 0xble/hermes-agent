"""Loop revisions and post-turn settlement use the goal lifecycle's snapshot/CAS contract.

Like goals_evaluation, stage changes on a clone and compare canonical serialized
state in SessionDB._execute_write before committing. The mutation token detects
ABA changes even when a pause/resume restores otherwise identical fields.
"""

from __future__ import annotations

import copy
import time
import uuid
from typing import Any

from hermes_cli.loops import LoopState, _get_session_db, _meta_key, load_loop


class LoopSnapshotChanged(RuntimeError):
    """The durable loop changed while a revision or judge was in flight."""


def _canonical(raw: str | None) -> str | None:
    return LoopState.from_json(raw).to_json() if raw else None


def assert_loop_snapshot(session_id: str, expected: str | None, db: Any) -> None:
    if _canonical(db.get_meta(_meta_key(session_id))) != expected:
        raise LoopSnapshotChanged("loop state changed")


def _commit(session_id: str, expected: str | None, state: LoopState, db: Any) -> None:
    def write(cursor):
        row = cursor.execute(
            "SELECT value FROM state_meta WHERE key = ?", (_meta_key(session_id),)
        ).fetchone()
        if _canonical(row[0] if row else None) != expected:
            raise LoopSnapshotChanged("loop state changed")
        state.mutation_id = uuid.uuid4().hex
        db.set_meta(_meta_key(session_id), state.to_json(), cursor=cursor)
    db._execute_write(write)


def _release_superseded_tick(manager: Any, original: LoopState | None, db: Any) -> None:
    """Release only the SAME tick on the winner; never apply its obsolete verdict.

    A revision/replace may preserve awaiting_response. Leaving that claim behind
    would wedge the loop. Pauses, clears, a new loop or a newer tick are untouched.
    Use another CAS, so even the cleanup cannot overwrite a second concurrent edit.
    """
    winner = load_loop(manager.session_id)
    manager._state = winner
    if original is None or winner is None or winner.status != "active" or not winner.awaiting_response:
        return
    identity = lambda state: (state.created_at, state.last_fired_at, state.ticks_fired)
    if identity(winner) != identity(original):
        return
    expected = winner.to_json()
    updated = LoopState.from_json(expected)
    updated.awaiting_response = False
    updated.next_due_at = time.time() + updated.current_delay
    try:
        _commit(manager.session_id, expected, updated, db)
        manager._state = updated
    except LoopSnapshotChanged:
        manager._state = load_loop(manager.session_id)


def mutate_loop_snapshot(manager: Any, method: str, *args, **kwargs) -> dict[str, Any]:
    """Stage a revision or settlement without persisting intermediate states."""
    settlement = method == "_complete_tick"
    if settlement:
        manager.refresh()
    original = manager.state
    db = _get_session_db() if manager.session_id else None
    if db is None or original is None:
        # Keep ephemeral behavior and the existing no-loop validation errors.
        return getattr(manager, method)(*args, **kwargs)
    expected = _canonical(db.get_meta(_meta_key(manager.session_id)))
    try:
        durable = LoopState.from_json(expected) if expected else None
        if durable is None or original.mutation_id != durable.mutation_id:
            raise LoopSnapshotChanged("loop state changed")
        staged = copy.copy(manager)
        staged._state = LoopState.from_json(original.to_json())
        staged._evaluation_snapshot = (db, expected)
        result = getattr(staged, method)(*args, **kwargs)
        if not settlement and not result.get("ok"):
            assert_loop_snapshot(manager.session_id, expected, db)
            return result
        if staged.state is not None and staged.state.to_json() != expected:
            _commit(manager.session_id, expected, staged.state, db)
        else:
            assert_loop_snapshot(manager.session_id, expected, db)
        # Preserve the existing state-reference API, but publish memory changes only
        # after the durable CAS succeeds (a losing revision never leaks its edits).
        original.__dict__.update(staged.state.__dict__)
        manager._state = original
        return result
    except LoopSnapshotChanged:
        if settlement:
            _release_superseded_tick(manager, original, db)
            return {"stopped": False, "status": manager.state.status if manager.state else "none",
                    "message": ""}
        manager._state = load_loop(manager.session_id)
        return manager._revision_result(ok=False, revision=None,
                                        version=manager.state.version if manager.state else 0,
                                        error_code="state_changed",
                                        error="loop changed; retry against the current version")
