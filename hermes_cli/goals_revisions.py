"""Versioned goal revisions, staged against the evaluator's durable snapshot contract."""

from __future__ import annotations

import copy
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def revise_goal_snapshot(manager: Any, **kwargs: Any) -> Dict[str, Any]:
    from hermes_cli.goals import GoalState, _get_session_db
    from hermes_cli.goals_evaluation import (
        GoalEvaluationConflict, GoalEvaluationPersistenceError, _reload_if_readable,
        assert_goal_snapshot, commit_goal_snapshot, _read_goal,
    )

    from hermes_cli.lifecycle_snapshot import commit_and_publish_snapshot

    replace_mode = bool(kwargs.pop("_replace", False))
    complete_mode = bool(kwargs.pop("_complete", False))

    def apply(target):
        method = target._complete if complete_mode else target._replace if replace_mode else target._revise
        return method(**kwargs)
    if (replace_mode and kwargs.get("actor") == "user" and manager.session_id
            and getattr(manager, "_cursor", None) is None):
        return _replace_from_durable_row(manager, **kwargs)
    # ``replace`` historically returns a structured ``no_goal`` result, while ordinary
    # revisions retain ``_require_goal``'s programmer-facing error for a missing goal.
    original = manager._state if replace_mode else manager._require_goal()
    if replace_mode and original is None:
        return manager._replace(**kwargs)
    if not manager.session_id:
        return apply(manager)
    if getattr(manager, "_cursor", None) is not None:
        # The caller already holds the write transaction and the manager loaded its state through
        # that cursor, so the snapshot is current and writers are serialized. Re-entering the
        # SessionDB lock for a separate read/CAS here would deadlock (session_controls.apply).
        return apply(manager)
    expected = original.to_json()
    db = None
    try:
        try:
            db = _get_session_db()
        except Exception as exc:
            raise GoalEvaluationPersistenceError from exc
        if db is None:
            raise GoalEvaluationPersistenceError("session DB unavailable")
        assert_goal_snapshot(manager.session_id, expected, db)
        staged = copy.copy(manager)
        staged._state = GoalState.from_json(expected)
        staged._evaluation_snapshot = (db, expected)
        result = apply(staged)
        # User authority can race independently of the goal row. Recheck a removal quote
        # under the same writer lock as the contract delta.
        from hermes_cli.goals_autonomy import validate_removal_quote
        def precondition(cursor):
            validate_removal_quote(staged.state, expected, cursor)
        # No-op and validation returns must also observe conflicting command writes.
        commit_and_publish_snapshot(
            manager, staged.state,
            lambda: commit_goal_snapshot(manager.session_id, expected, staged.state, db, precondition=precondition),
            lambda: _read_goal(manager.session_id, db),
        )
        return result
    except GoalEvaluationConflict:
        _reload_if_readable(manager, db)
        return {"ok": False, "error_code": "state_changed",
                "error": "goal changed; retry against the current version"}
    except GoalEvaluationPersistenceError:
        logger.warning("Goal revision could not read or commit durable state for %s",
                       manager.session_id, exc_info=True)
        _reload_if_readable(manager, db)
        return {"ok": False, "error_code": "persistence_failed",
                "error": "goal state could not be read or saved; revision was not applied"}


_USER_REPLACE_ATTEMPTS = 3
_STATE_CHANGED = {"ok": False, "error_code": "state_changed",
                  "error": "goal changed; retry against the current version"}


def _replace_from_durable_row(manager: Any, **kwargs: Any) -> Dict[str, Any]:
    """Apply an explicit user replacement to the CURRENT durable row through the CAS commit.

    The user command targets whatever goal is live, so a stale manager (the gateway loads it and
    dispatches in separate hops) must neither refuse it nor undo a concurrent verdict. The staged
    copy starts from a fresh durable read, upgraded by the legacy-requirement migration exactly as
    ``load_goal`` would, keeps that row's lifecycle fields, and commits only if the row is still
    unchanged. A racing writer forces a bounded re-read. Persistence failures leave the caller's
    state untouched and report that nothing was saved.
    """
    from hermes_cli.goals import (
        GoalRowCorrupt, _get_session_db, _meta_key, _migrate_legacy_revision_requirements,
        _parse_goal_row,
    )
    from hermes_cli.goals_evaluation import (
        GoalEvaluationConflict, GoalEvaluationPersistenceError, _read_goal, commit_goal_snapshot,
    )
    from hermes_cli.lifecycle_snapshot import commit_and_publish_snapshot

    not_saved = {"ok": False, "error_code": "persistence_failed",
                 "error": "goal change was not saved; try again"}
    try:
        db = _get_session_db()
    except Exception:
        db = None
    if db is None:
        return not_saved
    for _ in range(_USER_REPLACE_ATTEMPTS):
        try:
            raw = db.get_meta(_meta_key(manager.session_id))
        except Exception:
            logger.warning("Goal replace could not read %s", manager.session_id, exc_info=True)
            return not_saved
        if not raw:
            return manager._replace(**kwargs)   # structured no-goal result; nothing is written
        try:
            live = _parse_goal_row(manager.session_id, raw)
        except GoalRowCorrupt as exc:
            return {"ok": False, "error_code": "goal_unreadable", "error": str(exc)}
        expected = live.to_json()
        # Never replace an unmigrated legacy row as-is: its history-backed requirements would be
        # lost from this commit and re-applied to the new objective on the next load. The
        # migration and the replacement commit together, or not at all.
        _migrate_legacy_revision_requirements(live)
        staged = copy.copy(manager)
        staged._state = live
        staged._evaluation_snapshot = (db, expected)
        try:
            result = staged._replace(**kwargs)
        except GoalEvaluationConflict:
            continue
        except GoalEvaluationPersistenceError:
            return not_saved
        if not result.get("ok"):
            return result
        try:
            won = commit_and_publish_snapshot(
                manager, staged.state,
                lambda: commit_goal_snapshot(manager.session_id, expected, staged.state, db),
                lambda: _read_goal(manager.session_id, db),
            )
        except GoalEvaluationConflict:
            continue
        except GoalEvaluationPersistenceError:
            logger.warning("Goal replace for %s was not saved", manager.session_id, exc_info=True)
            return not_saved
        if manager.state is not None:
            # The durable row is migrated now; a cached pending marker must not keep refusing.
            manager.state.__dict__.pop("_legacy_migration_pending", None)
        if not won:
            return dict(_STATE_CHANGED)
        result["state"] = manager.state
        return result
    return dict(_STATE_CHANGED)


def stop_condition_only(state: Any, *, goal: str, contract: Any,
                        max_turns: Optional[int] = None) -> bool:
    """A stop-condition edit cannot replace the objective or any completion requirement."""
    from hermes_cli.goals import GoalContract

    if state is None or contract is None:
        return False
    before = state.contract.to_dict()
    after = (contract.to_dict() if isinstance(contract, GoalContract)
             else GoalContract.from_dict({**before, **contract}).to_dict())
    return (goal == state.goal and bool(before["stop_when"])
            and before["stop_when"] != after["stop_when"]
            and all(after[key] == value for key, value in before.items() if key != "stop_when")
            and (max_turns is None or max_turns == state.max_turns))


class GoalRevisionsMixin:
    def revise(self, *, reason: str, actor: str = "agent", goal: Optional[str] = None,
               contract: Optional[Dict[str, str]] = None, subgoals: Optional[List[str]] = None,
               evidence: str = "", user_quote: str = "",
               user_messages: Optional[List[str]] = None) -> Dict[str, Any]:
        """Apply an additive revision through the durable snapshot/CAS boundary.

        Default mode is additive-only and quotes do not grant removal authority. Opt-in autonomy
        allows objective edits while preserving requirements unless an exact recorded removal
        quote authorizes the delta. Stale revisions preserve the durable winner and return
        ``state_changed``. ``user_messages`` is compatibility input, never authority.
        """
        return revise_goal_snapshot(
            self, reason=reason, actor=actor, goal=goal, contract=contract,
            subgoals=subgoals, evidence=evidence, user_quote=user_quote,
        )

    def replace(self, *, reason: str, goal: str, max_turns: Optional[int] = None,
                contract: Optional[Any] = None, actor: str = "agent", user_quote: str = "",
                user_messages: Optional[List[str]] = None) -> Dict[str, Any]:
        """Replace through the durable snapshot/CAS boundary."""
        return revise_goal_snapshot(self, reason=reason, goal=goal, max_turns=max_turns,
                                    contract=contract, actor=actor, user_quote=user_quote,
                                    user_messages=user_messages, _replace=True)

    def _revise(self, *, reason: str, actor: str = "agent", goal: Optional[str] = None,
                contract: Optional[Dict[str, str]] = None, subgoals: Optional[List[str]] = None,
                evidence: str = "", user_quote: str = "") -> Dict[str, Any]:
        """Validate and apply an additive revision to this manager's (staged) state."""
        from hermes_cli.goals_autonomy import autonomous_edit, goal_autonomy_enabled
        if goal_autonomy_enabled() and actor == "agent":
            return autonomous_edit(self, reason=reason, goal=goal, contract=contract,
                                   subgoals=subgoals, user_quote=user_quote)
        state = self._require_goal()
        reason = (reason or "").strip()
        if not reason:
            return {"ok": False, "error_code": "reason_required", "error": "a revision needs a reason"}
        evidence = str(evidence or "").strip()
        # The transcript read runs before the CAS commit, which still rejects a racing write.
        evidence_error = self._recorded_evidence_error(evidence, state) if evidence else None
        result = self._revise_state(state, reason=reason, actor=actor, goal=goal, contract=contract,
                                    subgoals=subgoals, evidence=evidence, evidence_error=evidence_error)
        if result.get("ok"):
            self._save()
        return result

    def _replace(self, *, reason: str, goal: str, max_turns: Optional[int] = None,
                 contract: Optional[Any] = None, actor: str = "agent", user_quote: str = "",
                 user_messages: Optional[List[str]] = None) -> Dict[str, Any]:
        """Replace the objective in place, keeping lifecycle status and turn accounting.

        Only an explicit user command or approved session control (``actor="user"``) may
        replace; ``user_quote``/``user_messages`` never grant authority. A pause the judge set over
        the superseded contract (blocked/disputed) is lifted; every other pause stays.
        """
        from hermes_cli.goals import (
            GoalContract, _paused_by_contract_verdict, goal_remove_usage, normalize_goal_max_turns,
        )

        from hermes_cli.goals_autonomy import autonomous_edit, goal_autonomy_enabled
        if goal_autonomy_enabled() and actor == "agent":
            return autonomous_edit(self, reason=reason, goal=goal, contract=contract,
                                   max_turns=max_turns, user_quote=user_quote, kind="replace")
        if actor != "user":
            return {"ok": False, "error_code": "destructive_change_requires_command",
                    "error": f"Use goal_set action=replace through session controls with a current user instruction "
                             f"or approval, or an explicit /goal replace <text> or {goal_remove_usage()}. "
                             "Goal unchanged and still running."}
        state = self._state
        if state is None or state.status not in {"active", "paused"}:
            return {"ok": False, "error_code": "no_goal",
                    "error": "there is no active or paused goal to replace", "state": state, "revision": None}
        reason = (reason or "").strip()
        goal = (goal or "").strip()
        if not reason:
            return {"ok": False, "error_code": "reason_required",
                    "error": "a replacement needs a reason", "state": state, "revision": None}
        if not goal:
            return {"ok": False, "error_code": "no_change",
                    "error": "replacement goal is empty", "state": state, "revision": None}
        try:
            from hermes_cli.session_controls import _CURRENT_AUTHORITY
            authority = _CURRENT_AUTHORITY.get() or {}
        except Exception:
            authority = {}
        approved = authority.get("via") in {"quote", "button", "session"}

        def snapshot() -> Dict[str, Any]:
            return {"goal": state.goal, "max_turns": state.max_turns, "contract": state.contract.to_dict(),
                    "status": state.status, "turns_used": state.turns_used, "subgoals": list(state.subgoals)}

        previous = snapshot()
        lift_judge_pause = _paused_by_contract_verdict(state)
        state.goal = goal
        state.created_at = max(time.time(), float(state.created_at or 0.0) + 1e-6)
        if contract is not None:
            # A sparse edit must not turn omission into user-authorized removal. Typed
            # GoalContract values are complete contracts, mappings patch only named fields.
            state.contract = (contract if isinstance(contract, GoalContract)
                              else GoalContract.from_dict({**state.contract.to_dict(), **contract}))
        # Replacing an objective does not remove independent additive criteria. Their
        # existing explicit user removal controls remain the only removal path.
        if max_turns is not None:
            state.max_turns = normalize_goal_max_turns(max_turns, self.default_max_turns)
        # Status and turns_used are kept. Only a judge pause over the superseded contract lifts;
        # user, interrupt, budget, session-control and judge-unreachable pauses stay.
        if lift_judge_pause:
            state.status = "active"
            state.paused_reason = None
            state.clear_wait()
        # Per-objective judge bookkeeping restarts with the new objective.
        state.consecutive_parse_failures = 0
        state.consecutive_transport_failures = 0
        state.consecutive_no_progress = 0
        state.backoff_level = 0
        state.last_progress_fingerprint = None
        state.last_verdict = None
        state.last_reason = None
        revision = {"at": time.time(), "actor": "user", "kind": "replace", "reason": reason,
                    "authority": authority.get("via") if approved else "explicit_command",
                    "user_quote": user_quote if approved else "",
                    "user_message": str(authority.get("message") or "") if approved else "",
                    "before": previous, "after": snapshot()}
        if authority.get("via") == "button":
            revision["approved_by"] = str(authority.get("user_id") or "")
            revision["user_message"] = f'Approved in Telegram: replace goal with "{goal}"'
        state.revisions.append(revision)
        self._save()
        return {"ok": True, "error_code": "", "error": "", "state": state,
                "previous_goal": previous["goal"], "revision": revision, "version": 1}

    # --- /subgoal user controls ---------------------------------------
