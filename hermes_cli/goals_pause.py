"""Ownership-fenced goal pauses without holding the gateway generation lock over I/O."""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from hermes_cli.goals import GoalState


def pause_goal_if_current(
    session_id: str, expected_json: str, *, reason: str, is_current: Callable[[], bool],
) -> Optional["GoalState"]:
    """CAS-pause a loaded goal, linearizing ownership after acquiring SQLite's writer.

    The caller holds its route lock off-loop. ``is_current`` may briefly hold the generation
    lock, but must do no I/O. Once it accepts, the transaction orders this pause before any
    successor goal write; the loop may claim a new generation while SQLite persists it.
    """
    from hermes_cli.goals import GoalState, _get_session_db, _meta_key

    db = _get_session_db()
    if not session_id or db is None:
        return None
    key = _meta_key(session_id)

    def _txn(conn) -> Optional[GoalState]:
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is None or not row[0]:
            return None
        state = GoalState.from_json(row[0])
        if state.to_json() != expected_json or not is_current():
            return None
        state.status = "paused"
        state.paused_reason = reason
        state.clear_wait()
        state.consecutive_no_progress = 0
        state.backoff_level = 0
        state.mutation_id = uuid.uuid4().hex
        conn.execute("UPDATE state_meta SET value = ? WHERE key = ?", (state.to_json(), key))
        return state

    # Errors propagate to the gateway's existing best-effort stop boundary, not a false receipt.
    return db._execute_write(_txn)
