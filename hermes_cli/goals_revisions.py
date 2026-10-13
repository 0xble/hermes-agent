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
        assert_goal_snapshot, commit_goal_snapshot,
    )

    original = manager._require_goal()
    if not manager.session_id:
        return manager._revise(**kwargs)
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
        result = staged._revise(**kwargs)
        # No-op and validation returns must also observe conflicting command writes.
        commit_goal_snapshot(manager.session_id, expected, staged.state, db)
        original.__dict__.update(staged.state.__dict__)
        manager._state = original
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


class GoalRevisionsMixin:
    def revise(self, *, reason: str, actor: str = "agent", goal: Optional[str] = None,
               contract: Optional[Dict[str, str]] = None, subgoals: Optional[List[str]] = None,
               user_quote: str = "", user_messages: Optional[List[str]] = None) -> Dict[str, Any]:
        """Record a versioned revision of the goal, contract fields and/or subgoal list.

        ``contract`` updates only the named fields (``""`` clears one); ``subgoals`` replaces the list.
        A change to the objective or constraints, or a dropped subgoal, needs ``user_quote``: a verbatim
        excerpt (12+ chars) of a real user message in ``user_messages`` (defaults to this session's
        user messages since the goal was set). Returns ``{"ok", "error_code", "error", "revision"}``.
        Stale revisions preserve the durable winner and return ``state_changed``.
        """
        return revise_goal_snapshot(
            self, reason=reason, actor=actor, goal=goal, contract=contract,
            subgoals=subgoals, user_quote=user_quote, user_messages=user_messages,
        )

    # --- revisions ------------------------------------------------------

    # Changes that need an identifiable user instruction: the objective itself, dropping criteria,
    # and changing constraints. Other contract fields may be restructured by the agent; the judge
    # still holds an unauthorized revision to any earlier requirement it weakened.
    _AUTHORITY_FIELDS = ("goal", "constraints")

    def _revise(self, *, reason: str, actor: str = "agent", goal: Optional[str] = None,
               contract: Optional[Dict[str, str]] = None, subgoals: Optional[List[str]] = None,
               user_quote: str = "", user_messages: Optional[List[str]] = None) -> Dict[str, Any]:
        """Record a versioned revision of the goal, contract fields and/or subgoal list.

        ``contract`` updates only the named fields (``""`` clears one); ``subgoals`` replaces the list.
        A change to the objective or constraints, or a dropped subgoal, needs ``user_quote``: a verbatim
        excerpt (12+ chars) of a real user message in ``user_messages`` (defaults to this session's
        user messages since the goal was set). Returns ``{"ok", "error_code", "error", "revision"}``."""
        from hermes_cli.goals import (
            GoalContract, _CONTRACT_FIELDS, _REVISION_QUOTE_MIN_CHARS,
            _REVISION_SOURCE_MAX_CHARS, user_messages_since,
        )
        state = self._require_goal()
        reason = (reason or "").strip()
        if not reason:
            return {"ok": False, "error_code": "reason_required", "error": "a revision needs a reason"}
        before = {"goal": state.goal, **state.contract.to_dict(), "subgoals": list(state.subgoals)}
        after = dict(before)
        if goal is not None and goal.strip():
            after["goal"] = goal.strip()
        for key, value in (contract or {}).items():
            if key not in _CONTRACT_FIELDS:
                return {"ok": False, "error_code": "unknown_field", "error": f"unknown contract field: {key}"}
            after[key] = str(value or "").strip()
        if subgoals is not None:
            after["subgoals"] = [str(s).strip() for s in subgoals if str(s).strip()]
        changed = [k for k in after if after[k] != before[k]]
        if not changed:
            return {"ok": False, "error_code": "no_change", "error": "the revision changes nothing"}
        dropped = [s for s in before["subgoals"] if s not in after["subgoals"]]
        needs_authority = [k for k in self._AUTHORITY_FIELDS if k in changed] + (["subgoals"] if dropped else [])
        quote = " ".join((user_quote or "").split())
        if needs_authority and len(quote) < _REVISION_QUOTE_MIN_CHARS:
            return {"ok": False, "error_code": "user_authority_required",
                    "error": f"changing {', '.join(needs_authority)} needs user_quote: a verbatim excerpt "
                             f"({_REVISION_QUOTE_MIN_CHARS}+ chars) of the user's instruction in this session"}
        source = ""
        if quote:
            # Deterministic part: the quote must come from a real user message. Whether that message
            # authorizes this specific change is judged against the full message, shown with the revision.
            if len(quote) < _REVISION_QUOTE_MIN_CHARS:
                return {"ok": False, "error_code": "user_quote_too_short",
                        "error": f"user_quote must be at least {_REVISION_QUOTE_MIN_CHARS} characters"}
            pool = user_messages if user_messages is not None else user_messages_since(self.session_id, state.created_at)
            sources = [" ".join(m.split()) for m in pool if quote in " ".join(m.split())]
            if not sources:
                return {"ok": False, "error_code": "user_quote_not_found",
                        "error": "user_quote does not match any user message sent since the goal was set"}
            source = next((m for m in sources if len(m) <= _REVISION_SOURCE_MAX_CHARS), "")
            if not source:
                return {"ok": False, "error_code": "user_message_too_long",
                        "error": f"the quoted user message exceeds {_REVISION_SOURCE_MAX_CHARS} characters, too "
                                 "long to judge whether it authorizes this change; ask the user to state the "
                                 "change in a short message and quote that"}
        revision = {"at": time.time(), "actor": actor, "reason": reason, "user_quote": quote,
                    "user_message": source,
                    "before": {k: before[k] for k in changed}, "after": {k: after[k] for k in changed}}
        state.goal = after["goal"]
        state.contract = GoalContract.from_dict({k: after[k] for k in _CONTRACT_FIELDS})
        state.subgoals = after["subgoals"]
        state.revisions.append(revision)
        # The bar moved: an earlier dispute streak was about the superseded wording.
        state.consecutive_disputes = 0
        state.last_dispute_evidence = ""
        self._save()
        return {"ok": True, "revision": revision, "version": len(state.revisions) + 1}
