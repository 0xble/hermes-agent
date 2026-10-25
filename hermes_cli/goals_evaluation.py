"""Optimistic goal evaluation, with no database lock held over gates or model calls."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from hermes_cli.lifecycle_snapshot import commit_and_publish_snapshot, snapshot_control

if TYPE_CHECKING:
    from hermes_cli.goals import GoalManager, GoalState
    from hermes_state import SessionDB

logger = logging.getLogger(__name__)


class GoalEvaluationConflict(Exception):
    """A command or another evaluator changed the state being judged."""


class GoalEvaluationPersistenceError(Exception):
    """The evaluator could not read or commit durable state."""


def _read_goal(session_id: str, db: SessionDB) -> GoalState | None:
    from hermes_cli.goals import GoalState, _meta_key, goal_autonomy_enabled, load_goal

    try:
        if goal_autonomy_enabled():
            return load_goal(session_id, db=db)
        raw = db.get_meta(_meta_key(session_id))
        return GoalState.from_json(raw) if raw else None
    except Exception as exc:
        raise GoalEvaluationPersistenceError from exc


def assert_goal_snapshot(session_id: str, expected: str, db: SessionDB) -> None:
    current = _read_goal(session_id, db)
    if current is None or current.to_json() != expected:
        raise GoalEvaluationConflict


@snapshot_control
def _reload_if_readable(manager: GoalManager, db: SessionDB | None) -> None:
    if db is not None:
        try:
            manager._state = _read_goal(manager.session_id, db)
        except GoalEvaluationPersistenceError:
            # An unreadable DB is not evidence that the goal was deleted.
            logger.warning("Could not reload goal %s; retaining in-memory state", manager.session_id)


def commit_goal_snapshot(session_id: str, expected: str, state: GoalState, db: SessionDB, *, precondition=None) -> None:
    """Validate and commit in the SessionDB writer transaction, including unchanged results."""
    from hermes_cli.goals import GoalState, _meta_key

    def commit(cursor):
        row = cursor.execute("SELECT value FROM state_meta WHERE key = ?", (_meta_key(session_id),)).fetchone()
        if not row or not row[0] or GoalState.from_json(row[0]).to_json() != expected:
            raise GoalEvaluationConflict
        if precondition is not None:
            precondition(cursor)
        if state.to_json() != expected:
            state.mutation_id = uuid.uuid4().hex
            db.set_meta(_meta_key(session_id), state.to_json(), cursor=cursor)

    try:
        db._execute_write(commit)
    except GoalEvaluationConflict:
        raise
    except Exception as exc:
        raise GoalEvaluationPersistenceError from exc


def evaluate_goal_snapshot(manager: GoalManager, last_response: str, **kwargs: Any) -> dict[str, Any]:
    from hermes_cli.goals import GoalState, _decision, _get_session_db, goal_autonomy_enabled

    state = manager.state
    if state is None or state.status != "active":
        return _decision(state.status if state else None, False, None, "inactive", "no active goal", "")
    if goal_autonomy_enabled() and getattr(state, "_legacy_migration_pending", False):
        return _decision("active", False, None, "persistence_failed", "goal migration was not saved",
                         "Goal migration could not be saved. The next load will retry it.")
    if not manager.session_id:
        return manager._evaluate_after_turn(last_response, **kwargs)
    expected = state.to_json()
    db = None
    try:
        try:
            db = _get_session_db()
        except Exception as exc:
            raise GoalEvaluationPersistenceError from exc
        if db is None:
            raise GoalEvaluationPersistenceError("session DB unavailable")
        assert_goal_snapshot(manager.session_id, expected, db)
        # Commands can use the original manager while the judge runs. Only this
        # isolated evaluator stages writes, so those commands still persist normally.
        evaluator = object.__new__(type(manager))
        evaluator.__dict__ = manager.__dict__.copy()
        evaluator._state = GoalState.from_json(expected)
        evaluator._evaluation_snapshot = (db, expected)
        decision = evaluator._evaluate_after_turn(last_response, **kwargs)
        result = evaluator.state

        published = commit_and_publish_snapshot(
            manager, result, lambda: commit_goal_snapshot(manager.session_id, expected, result, db),
            lambda: _read_goal(manager.session_id, db), preserve_reference=False,
        )
        if not published:
            return _decision(
                manager.state.status if manager.state else None, False, None, "interrupted",
                "goal changed after evaluation committed", "",
            )
        return decision
    except GoalEvaluationConflict:
        _reload_if_readable(manager, db)
        return _decision(
            manager.state.status if manager.state else None, False, None, "interrupted",
            "goal changed during evaluation", "Goal state changed during evaluation; the current goal was preserved.",
        )
    except GoalEvaluationPersistenceError:
        logger.warning("Goal evaluation could not read or commit durable state for %s", manager.session_id, exc_info=True)
        _reload_if_readable(manager, db)
        return _decision(
            manager.state.status if manager.state else None, False, None, "persistence_failed",
            "goal state could not be read or saved", "Goal state could not be read or saved; automatic continuation stopped.",
        )


def blocked_goal_decision(manager, reason):
    from hermes_cli.goals import _BLOCKED_ON_USER_RE, _BLOCKED_PAUSE_RE, _BLOCKED_PAUSE_PREFIX

    # Only a real irreversible-constraint breach or a goal with nothing left to try
    # pauses. Every other BLOCKED (approval, decision, credentials, an answer) is a
    # wait on the user's next message: the goal stays active and no /goal resume chore.
    needs_user = bool(_BLOCKED_ON_USER_RE.search(reason or "")) and not _BLOCKED_PAUSE_RE.search(reason or "")
    if needs_user:
        return manager._apply_wait_directive({"user_message": True}, reason)
    return manager._pause_decision(
        f"{_BLOCKED_PAUSE_PREFIX}{reason}", "blocked", reason,
        f"⏸ Goal blocked — paused: {reason} If input is needed, supply it to continue; "
        "use /goal set to re-scope or /goal resume to override this block (counts as your approval of it).",
    )
