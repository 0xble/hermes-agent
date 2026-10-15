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
    # ``replace`` historically returns a structured ``no_goal`` result, while ordinary
    # revisions retain ``_require_goal``'s programmer-facing error for a missing goal.
    original = manager._state if replace_mode else manager._require_goal()
    if replace_mode and original is None:
        return manager._replace(**kwargs)
    if not manager.session_id:
        return manager._replace(**kwargs) if replace_mode else manager._revise(**kwargs)
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
        result = staged._replace(**kwargs) if replace_mode else staged._revise(**kwargs)
        # No-op and validation returns must also observe conflicting command writes.
        commit_and_publish_snapshot(
            manager, staged.state,
            lambda: commit_goal_snapshot(manager.session_id, expected, staged.state, db),
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

    def replace(self, *, reason: str, goal: str, max_turns: Optional[int] = None,
                contract: Optional[Any] = None, user_quote: str = "",
                user_messages: Optional[List[str]] = None) -> Dict[str, Any]:
        """Replace through the durable snapshot/CAS boundary."""
        return revise_goal_snapshot(self, reason=reason, goal=goal, max_turns=max_turns,
                                    contract=contract, user_quote=user_quote,
                                    user_messages=user_messages, _replace=True)

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

    def _replace(self, *, reason: str, goal: str, max_turns: Optional[int] = None,
                 contract: Optional[Any] = None, user_quote: str = "",
                 user_messages: Optional[List[str]] = None) -> Dict[str, Any]:
        """Replace the goal with explicit quote authority and preserve revision history."""
        from hermes_cli.goals import GoalState, GoalContract, _CONTRACT_FIELDS, normalize_goal_max_turns
        old = self._state
        if old is None or old.status not in {"active", "paused"}:
            return {"ok": False, "error_code": "no_goal", "error": "there is no active or paused goal to replace", "state": old, "revision": None}
        reason = (reason or "").strip()
        goal = (goal or "").strip()
        if not reason:
            return {"ok": False, "error_code": "reason_required", "error": "a replacement needs a reason", "state": old, "revision": None}
        if not goal:
            return {"ok": False, "error_code": "no_change", "error": "replacement goal is empty", "state": old, "revision": None}
        authority = None
        try:
            from hermes_cli.session_controls import _CURRENT_AUTHORITY
            authority = _CURRENT_AUTHORITY.get()
        except Exception:
            authority = None
        quote = " ".join((user_quote or "").split())
        source = ""
        if not (authority and authority.get("via") in {"quote", "button"}):
            if not quote:
                return {"ok": False, "error_code": "user_authority_required", "error": "goal replacement needs user_quote", "state": old, "revision": None}
            try:
                from hermes_cli.session_controls import (
                    _governing_sentence, check_user_quote, quote_matches_control)
                checked = check_user_quote(self.session_id, quote)
                if isinstance(checked, str):
                    return {"ok": False, "error_code": checked, "error": checked, "state": old, "revision": None}
                if not quote_matches_control("goal", "replace", self.session_id, self.session_id,
                                             _governing_sentence(*checked), {"goal": goal}):
                    return {"ok": False, "error_code": "user_quote_mismatch",
                            "error": "the quote must be the user asking to replace or change the goal",
                            "state": old, "revision": None}
                quote, source = checked
                # A quote validated here is the same user authority apply_control records.
                authority = {"via": "quote", "quote": quote, "message": source}
            except ValueError as exc:
                code = str(exc)
                return {"ok": False, "error_code": code, "error": code, "state": old, "revision": None}
        elif authority.get("via") == "button":
            # Button approval is the user's authority: record who approved what so the judge and
            # continuation treat the old goal as superseded rather than as an agent-only change.
            quote = ""
            source = f'Approved in Telegram: replace goal with "{goal}"'
        else:
            source = str(authority.get("message") or "")
        if user_messages is not None and source == "" and user_messages:
            source = " ".join(str(user_messages[-1]).split())
        old_contract = old.contract.to_dict()
        new_contract = contract.to_dict() if hasattr(contract, "to_dict") else dict(contract or {})
        new_contract = {k: str(new_contract.get(k) or "").strip() for k in _CONTRACT_FIELDS}
        before = {"goal": old.goal, "max_turns": old.max_turns, "contract": old_contract,
                  "status": old.status, "subgoals": list(old.subgoals)}
        after = {"goal": goal, "max_turns": old.max_turns if max_turns is None else normalize_goal_max_turns(max_turns, self.default_max_turns),
                 "contract": new_contract, "status": "active", "subgoals": []}
        revision = {"kind": "replace", "before": before, "after": after, "user_quote": quote,
                    "user_message": source, "reason": reason, "at": time.time(), "actor": "agent"}
        if authority and authority.get("via") in {"quote", "button"}:
            revision["actor"] = "user"
            revision["authority"] = authority["via"]
            if authority["via"] == "button":
                revision["approved_by"] = str(authority.get("user_id") or "")
        revisions = list(old.revisions) + [revision]
        self._state = GoalState(goal=goal, status="active", turns_used=0, created_at=self._next_instance_time(),
                                max_turns=after["max_turns"], contract=GoalContract.from_dict(new_contract),
                                revisions=revisions)
        self._save()
        return {"ok": True, "error_code": "", "error": "", "state": self._state,
                "revision": revision}

    # --- /subgoal user controls ---------------------------------------
