"""Isolated goal evaluation with a short, cross-process persistence fence."""
from __future__ import annotations

import copy
import hashlib
from typing import Callable

from hermes_cli import goals


class _GoalDraft(goals.GoalManager):
    def __init__(self, manager: goals.GoalManager, state: goals.GoalState):
        self.session_id = manager.session_id
        self.default_max_turns = manager.default_max_turns
        self._state = copy.deepcopy(state)
        self.changed = False

    def refresh(self):
        return self._state

    def _persist_state(self, state):
        self.changed = True
        return True


def state_token(raw: str | None) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def evaluate_draft(
    manager: goals.GoalManager, evaluate: Callable[[_GoalDraft], dict],
    *, expected_revision: int | None, is_current: Callable[[], bool],
) -> dict:
    if not is_current():
        return {}
    if expected_revision is None:
        expected_revision = goals.get_goal_control_revision(manager.session_id)
    cache_key = goals._goal_control_cache_key(manager.session_id)

    def unavailable(stage):
        # This is a report, never persistence or continuation authority. A revoked
        # turn still stays silent even if its outstanding database operation fails.
        if (not is_current() or goals._MODEL_GOAL_CONTROL_REVISIONS.get(
                cache_key, expected_revision) != expected_revision):
            return {}
        explanation = (
            f"Goal evaluation unavailable: {stage}. "
            "Completion has not been verified and automatic continuation was not authorized. "
            "No durable pause is confirmed; the stored goal may still be active. "
            "Next: restore goal storage, check /goal status, then explicitly direct further work."
        )
        decision = goals._decision("evaluation_failed", False, None, "error", stage, explanation)
        decision["stop_explanation"] = explanation
        return decision

    db = goals._get_session_db()
    if db is None:
        return unavailable("goal storage unavailable")
    key = goals._meta_key(manager.session_id)
    revision_key = goals._goal_control_revision_key(manager.session_id)
    try:
        baseline = db.get_meta_values([key, revision_key])
        raw = baseline[key]
        revision = int(baseline[revision_key] or 0)
        if expected_revision is not None and revision != expected_revision:
            manager.refresh()
            return {}
        if manager._has_stored_baseline and manager._stored_goal_raw == raw:
            state = manager._state
        else:
            state = goals.GoalState.from_json(raw) if raw else None
        if state is None or state.status != "active" or not is_current():
            manager.refresh()
            return {}
        draft = _GoalDraft(manager, state)
    except Exception as exc:
        goals.logger.warning("Goal evaluation snapshot unavailable: %s", type(exc).__name__)
        return unavailable("goal snapshot could not be read")

    decision = evaluate(draft)
    accepted_raw = draft._state.to_json() if draft.changed else raw
    try:
        # A failed revision persistence still invalidates local work. Read the local token
        # without acquiring the control lock here: controls acquire it before the DB lock.
        def commit_is_current():
            return (is_current()
                    and goals._MODEL_GOAL_CONTROL_REVISIONS.get(cache_key, revision) == revision)
        committed = db.compare_and_set_meta(
            baseline, {key: accepted_raw} if draft.changed else {}, is_current=commit_is_current)
    except Exception as exc:
        goals.logger.warning("Goal evaluation commit unavailable: %s", type(exc).__name__)
        return unavailable("goal evaluation could not be committed")
    # The live manager may already contain a later user control. Refresh against its own
    # accepted baseline instead of assigning the draft and erasing those local changes.
    manager.refresh()
    if not committed:
        return {}
    decision["_goal_authority"] = {
        "session_id": manager.session_id, "revision": revision,
        "updated_at": draft._state.updated_at, "state_token": state_token(accepted_raw),
    }
    return decision


def decision_is_current(decision: dict) -> bool:
    authority = decision.get("_goal_authority") or {}
    sid, token = authority.get("session_id"), authority.get("state_token")
    if not sid or not token:
        return False
    db = goals._get_session_db()
    if db is None:
        return False
    try:
        key, revision_key = goals._meta_key(sid), goals._goal_control_revision_key(sid)
        current = db.get_meta_values([key, revision_key])
        return (state_token(current[key]) == token
                and int(current[revision_key] or 0) == authority.get("revision")
                and goals.get_goal_control_revision(sid) == authority.get("revision"))
    except Exception:
        return False


def claim_transition_notice(manager: goals.GoalManager, decision: dict) -> bool:
    """Claim once without restoring a goal superseded by a concurrent control."""
    db = goals._get_session_db()
    if db is None:
        return False
    key = goals._meta_key(manager.session_id)
    revision_key = goals._goal_control_revision_key(manager.session_id)
    cache_key = goals._goal_control_cache_key(manager.session_id)
    try:
        baseline = db.get_meta_values([key, revision_key])
        raw = baseline[key]
        revision = int(baseline[revision_key] or 0)
        state = goals.GoalState.from_json(raw) if raw else None
        if state is None or state.status == "cleared":
            return False
        authority = decision.get("_goal_authority")
        if authority is not None and (
            authority.get("session_id") != manager.session_id
            or authority.get("revision") != revision
            or authority.get("state_token") != state_token(raw)
        ):
            return False
        notice_key = _transition_notice_key(state, decision)
        if not notice_key or state.last_notice_key == notice_key:
            return False
        state.last_notice_key = notice_key
        manager._touch_state(state)
        # Do not take the control lock inside the database transaction: controls
        # take these locks in the opposite order. Failed revision persistence
        # must still invalidate authority in the current process.
        return db.compare_and_set_meta(
            baseline, {key: state.to_json()},
            is_current=lambda: goals._MODEL_GOAL_CONTROL_REVISIONS.get(cache_key, revision) == revision,
        )
    except Exception as exc:
        goals.logger.warning("Goal notice claim unavailable: %s", type(exc).__name__)
        return False
    finally:
        # Never replace local controls with the detached notice snapshot.
        manager.refresh()


def _transition_notice_key(state: goals.GoalState, decision: dict) -> str:
    transition = str(decision.get("transition") or "")
    if not transition:
        if decision.get("verdict") == "done":
            transition = "done"
        elif decision.get("verdict") == "blocked":
            transition = "blocked"
        elif decision.get("verdict") == "gate_failed":
            transition = "gate_failed"
        elif decision.get("status") == "paused":
            transition = "paused"
    if not transition:
        return ""
    if transition == "waiting":
        target = state.waiting_on_delegation or state.waiting_on_session or state.waiting_on_pid or state.waiting_until
        key = f"waiting:{target}:{state.waiting_since}"
    else:
        key = f"{transition}:{decision.get('reason') or ''}"
    return key
