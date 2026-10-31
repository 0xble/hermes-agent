"""Opt-in autonomous transitions on the native goal persistence boundary."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Mapping


AUTONOMY_CONTINUATION_TEMPLATE = (
    "[Continuing toward your standing goal] Goal: {goal}\n\n"
    "Completion contract:\n{contract_block}\n{subgoals_block}\n\n"
    "Manage this goal autonomously. Choose the next concrete authorized step. If a route fails, "
    "try a new route. For an external dependency use a timed wait with an automatic wake, "
    "rather than stopping for input or approval. Preserve every constraint, hard limit and "
    "verification criterion when revising or replacing. Never use complete or replace to escape "
    "constraints, failing checks or unsolved work. Use goal_set complete only with an evidence "
    "mapping from EVERY verification criterion to concrete recorded tool output. Never clear a goal. "
    "If nothing material changed, reply exactly [SILENT]."
)


def autonomy_enabled(section: str) -> bool:
    """Whether ``<section>.autonomy`` is on, for the goals and loops lifecycles."""
    # Resolve at call time under the owning profile, including idle wake scans.
    from hermes_cli.config_effective import load_user_config_effective

    return (load_user_config_effective().get(section) or {}).get("autonomy") is True


def goal_autonomy_enabled() -> bool:
    return autonomy_enabled("goals")


def loop_autonomy_enabled() -> bool:
    return autonomy_enabled("loops")


def completion_criteria(state) -> list[str]:
    from hermes_cli.goals import contract_items

    items = contract_items("verification", state.contract.verification) or [state.contract.outcome or state.goal]
    return list(dict.fromkeys([*items, *contract_items("subgoals", state.subgoals)]))


def record_hard_limit_breach(state, reason: str) -> None:
    from hermes_cli.goals_authority import _evidence_is_negated

    breached = re.search(
        r"(?i)\b(?:constraints?|hard[- ]limits?)\s+(?:(?:was|were|has been|have been)\s+)?"
        r"(?:violated|breached|violation|breach)\b|"
        r"\b(?:violated|breached)\b[^.;]{0,40}\b(?:constraints?|hard[- ]limits?)\b", reason or "")
    if breached and _evidence_is_negated(reason, breached.group(0), include_no=True):
        return
    if breached and not any(
            row.get("kind") == "hard_limit_breach" and row.get("reason") == reason
            for row in state.revisions):
        state.revisions.append({"at": time.time(), "kind": "hard_limit_breach", "reason": reason,
                                "actor": "system", "authority": "autonomy"})


def migrate_autonomous_goal(state) -> bool:
    if not goal_autonomy_enabled():
        return False
    if state.status not in {"active", "paused", "blocked", "needs-input", "needs_input",
                             "awaiting-approval", "awaiting_approval"}:
        return False
    system_pause = state.status in {"blocked", "needs-input", "needs_input", "awaiting-approval", "awaiting_approval"} or (
        state.status == "paused" and
        (state.paused_reason or "").startswith(("turn budget exhausted", "quality gate", "judge ", "judged unachievable:")))
    untimed_external = state.status == "active" and (state.waiting_on_pid or state.waiting_on_session) and not state.waiting_until
    if not system_pause and not state.waiting_on_user_message and not untimed_external:
        return False
    reason = state.last_reason or state.paused_reason or "legacy input wait"
    if (state.paused_reason or "").startswith("turn budget exhausted"):
        state.revisions.append({"at": time.time(), "kind": "budget_checkpoint", "reason": reason,
                                "turns_used": state.turns_used, "actor": "system", "authority": "autonomy"})
        state.turns_used = 0
    if state.last_verdict == "blocked":
        record_hard_limit_breach(state, reason)
    state.status = "active"
    state.paused_reason = None
    state.last_verdict = "continue"
    state.clear_wait(preserve_notice_key=True)
    state.waiting_since = time.time()
    state.waiting_until = state.waiting_since + 60
    state.waiting_seconds = 60
    state.waiting_reason = "Autonomy enabled: reconsider the route"
    state.revisions.append({"at": time.time(), "kind": "autonomy_migration", "reason": reason,
                            "actor": "system", "authority": "autonomy"})
    return True


def completion_proof_is_concrete(proof: str) -> bool:
    from hermes_cli.goals import _REVISION_EVIDENCE_MIN_CHARS

    excerpt = " ".join(proof.split())
    if re.fullmatch(r"(?:[0-9a-fA-F]{7,64}|https?://\S+|\d+\s+passed)", excerpt):
        return True
    words = re.findall(r"\w+", excerpt.casefold())
    generic = {"ok", "okay", "success", "successful", "successfully", "all", "checks",
               "passed", "pass", "done", "complete", "completed"}
    return len(excerpt) >= _REVISION_EVIDENCE_MIN_CHARS and len(words) >= 2 and bool(set(words) - generic)


def current_removal_quote(session_id, quote, *, cursor=None):
    from hermes_cli.goals_authority import _authority_unit_records, _REMOVAL_VERBS
    from hermes_cli.session_controls import _read_current_user_quote, _message_grants_authority, _normalize

    checked = _read_current_user_quote(session_id, quote, cursor=cursor)
    if isinstance(checked, str):
        return checked
    normalized, source = checked
    # The entire current instruction must authorize removal. Quoted item names are
    # data, so their own words (such as "Never restart") cannot negate the directive.
    if normalized != _normalize(source):
        return "user_quote_qualified"
    units = _authority_unit_records(source)
    if not units:
        return "user_quote_qualified"
    for direct, targets, _original in units:
        if not _message_grants_authority(direct):
            return "user_quote_qualified"
        if targets:
            grammar = rf"\s*(?:please\s+)?{_REMOVAL_VERBS}\s+(?:(?:the\s+)?(?:constraint|criterion|requirement)\s*)?[.!;\s]*"
        else:
            grammar = rf"\s*(?:please\s+)?{_REMOVAL_VERBS}\s+.+"
        if not re.fullmatch(grammar, direct, re.IGNORECASE | re.DOTALL):
            return "user_quote_qualified"
    return checked


def autonomous_edit(manager, *, reason, goal=None, contract=None, subgoals=None,
                    max_turns=None, user_quote="", kind="revise"):
    """Validate the entire delta before changing the staged snapshot."""
    from hermes_cli.goals import GoalContract, contract_items
    from hermes_cli.goals_authority import (
        _normalize_authority_value, _removal_instruction_units,
    )
    from hermes_cli.session_controls import _CURRENT_AUTHORITY

    state = manager.state
    if state is None or state.status not in {"active", "paused"}:
        return {"ok": False, "error_code": "no_goal", "error": "no active or paused goal"}
    if not str(reason or "").strip():
        return {"ok": False, "error_code": "reason_required", "error": "a revision needs a reason"}
    before = {"goal": state.goal, **state.contract.to_dict(), "subgoals": list(state.subgoals),
              "max_turns": state.max_turns}
    # A goal without explicit verification still has an implicit completion bar.
    # Materialize it before editing so adding a tiny check or changing the objective
    # cannot retire the unmet original outcome.
    if not contract_items("verification", before["verification"]):
        before["subgoals"] = list(dict.fromkeys([*before["subgoals"], before["outcome"] or before["goal"]]))
    after = dict(before)
    after.update(contract.to_dict() if isinstance(contract, GoalContract) else dict(contract or {}))
    if goal is not None:
        if not str(goal).strip():
            return {"ok": False, "error_code": "invalid_goal", "error": "goal text is empty"}
        after["goal"] = goal.strip()
    if subgoals is not None:
        after["subgoals"] = contract_items("subgoals", subgoals)
    if max_turns is not None:
        from hermes_cli.goals import normalize_goal_max_turns
        after["max_turns"] = normalize_goal_max_turns(max_turns)
    if set(after) != set(before):
        return {"ok": False, "error_code": "unknown_field", "error": "unknown contract field"}
    removed = [item for field in (*state.contract.to_dict(), "subgoals")
               for item in contract_items(field, before[field])
               if item not in contract_items(field, after[field])]
    if after["max_turns"] != before["max_turns"]:
        removed.append("turn budget")
    if removed:
        authority = _CURRENT_AUTHORITY.get() or {}
        source_sid = authority.get("session_id") or manager.session_id
        checked = current_removal_quote(source_sid, user_quote, cursor=manager._cursor) if user_quote else "missing"
        targets = [] if isinstance(checked, str) else [
            _normalize_authority_value(target)
            for _verb, target, quoted in _removal_instruction_units(checked[0])
            for target in [target, *quoted]]
        missing = [item for item in removed if _normalize_authority_value(item) not in targets]
        if missing:
            return {"ok": False, "error_code": "constraint_quote_required",
                    "error": "Retain each requirement or quote the user's exact removal instruction: " + "; ".join(missing)}
    if before == after:
        return {"ok": False, "error_code": "no_change", "error": "the revision changes nothing"}
    state.goal = after["goal"]
    state.contract = GoalContract.from_dict(after)
    state.subgoals = after["subgoals"]
    state.max_turns = after["max_turns"]
    # Replacement is a new continuation instance. Keep breach/contract history, but
    # require fresh proof of the new objective rather than accepting stale readbacks.
    if kind == "replace":
        state.created_at = max(time.time(), state.created_at + 1e-6)
    revision = {"at": time.time(), "actor": "agent", "authority": "autonomy", "kind": kind,
                "reason": reason, "before": before, "after": after,
                "user_quote": user_quote if removed else ""}
    if removed:
        revision["quote_session_id"] = source_sid
        revision["quote_message"] = checked[1]
    state.revisions.append(revision)
    manager._save()
    return {"ok": True, "revision": revision, "state": state, "version": len(state.revisions) + 1}


def validate_removal_quote(state, expected, cursor):
    from hermes_cli.goals_evaluation import GoalEvaluationConflict

    if len(state.revisions) <= len(json.loads(expected).get("revisions") or []):
        return
    revision = state.revisions[-1]
    if revision.get("authority") != "autonomy" or not revision.get("user_quote"):
        return
    checked = current_removal_quote(revision["quote_session_id"], revision["user_quote"], cursor=cursor)
    if isinstance(checked, str) or checked[1] != revision["quote_message"]:
        raise GoalEvaluationConflict


def autonomous_verdict(state, verdict, reason, wait_directive):
    if goal_autonomy_enabled() and verdict in {"blocked", "done"}:
        if verdict == "blocked":
            record_hard_limit_breach(state, reason)
            reason = "Choose a different route: " + reason
        else:
            reason = "Use goal_set complete with evidence for every criterion"
        return "continue", reason, None
    return verdict, reason, wait_directive


class GoalAutonomyMixin:
    def _autonomy_wait_seconds(self):
        return max(900, math.ceil(self.min_continuation_gap_seconds))

    def _autonomy_budget_checkpoint(self, state, reason):
        state.revisions.append({"at": time.time(), "kind": "budget_checkpoint", "reason": reason,
                                "turns_used": state.turns_used, "actor": "system", "authority": "autonomy"})
        state.turns_used = 0
        return self._autonomy_retry(reason)

    def _gate_failure_prompt(self, gate, exit_code, output):
        from hermes_cli.goals import CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE

        if not goal_autonomy_enabled():
            return CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE.format(
                goal=self.state.goal, command=gate.command, exit_code=exit_code, attempt=gate.attempts,
                max_retries=gate.max_retries, output=output or "(no output)")
        block = (self.state.contract.render_block() + f"\nQuality gate failed: $ {gate.command}\n"
                 f"Exit code: {exit_code}\n{output or '(no output)'}\nRepair this gate without weakening it.")
        return AUTONOMY_CONTINUATION_TEMPLATE.format(
            goal=self.state.goal, contract_block=block, subgoals_block=self.state.render_subgoals_block())

    @staticmethod
    def _history_for_next_goal(previous):
        if not goal_autonomy_enabled() or previous is None or previous.status != "done":
            return []
        return [*previous.revisions, {"at": time.time(), "kind": "set", "actor": "agent",
                "authority": "autonomy", "reason": "Previous goal completed", "goal": previous.goal,
                "contract": previous.contract.to_dict()}]

    def _autonomy_timer_elapsed(self):
        state = self.state
        return (goal_autonomy_enabled() and state is not None
                and state.waiting_until > 0 and time.time() >= state.waiting_until)

    @staticmethod
    def _autonomy_budget_exhausted(state):
        return goal_autonomy_enabled() and state.max_turns > 0 and state.turns_used >= state.max_turns

    def complete(self, *, reason: str, evidence: Mapping[str, str]):
        from hermes_cli.goals_revisions import revise_goal_snapshot

        if not goal_autonomy_enabled():
            return {"ok": False, "error_code": "autonomy_disabled", "error": "completion is owned by the judge"}
        if not self.has_goal():
            return {"ok": False, "error_code": "no_goal", "error": "no active or paused goal"}
        return revise_goal_snapshot(self, reason=reason, evidence=evidence, _complete=True)

    def _complete(self, *, reason, evidence):
        state = self._require_goal()
        if state.pending_peers:
            return {"ok": False, "error_code": "pending_peers",
                    "error": "pending peer work requires parent verification before goal completion"}
        criteria = completion_criteria(state)
        gap = ""
        if not reason or not str(reason).strip():
            gap = "completion needs a reason"
        elif any(row.get("kind") == "hard_limit_breach" for row in state.revisions):
            gap = "recorded hard constraint breach prevents completion"
        elif not isinstance(evidence, Mapping):
            gap = "evidence must map every criterion to recorded proof: " + "; ".join(criteria)
        elif any(not isinstance(evidence.get(item), str) or not evidence[item].strip() for item in criteria):
            gap = "missing evidence for: " + "; ".join(
                item for item in criteria if not isinstance(evidence.get(item), str) or not evidence[item].strip())
        else:
            for criterion in criteria:
                proof = evidence.get(criterion)
                if not completion_proof_is_concrete(proof):
                    gap = criterion + ": cite concrete recorded proof, such as a measured result, readback, URL or SHA"
                    break
                failure = self._recorded_evidence_error(proof, state)
                if failure:
                    gap = criterion + ": " + failure["error"]
                    break
        if not gap:
            state.status = "active"
            gate_decision = self._check_gates()
            if gate_decision is not None:
                gap = gate_decision["reason"]
        if gap:
            state.status = "active"
            state.paused_reason = None
            state.revisions.append({"at": time.time(), "kind": "complete_refused", "reason": gap,
                                    "actor": "agent", "authority": "autonomy"})
            self._save()
            return {"ok": False, "error_code": "completion_unproven", "error": gap}
        state.clear_wait()
        state.status = "done"
        state.paused_reason = None
        state.last_verdict = "done"
        state.last_reason = reason
        state.revisions.append({"at": time.time(), "kind": "complete", "reason": reason,
                                "actor": "agent", "authority": "autonomy", "evidence": dict(evidence)})
        self._save()
        return {"ok": True, "state": state, "evidence": dict(evidence)}

    def _autonomy_retry(self, reason: str):
        from hermes_cli.goals import _decision

        self.state.last_verdict = "continue"
        self.state.last_reason = reason
        self.wait_for_seconds(self._autonomy_wait_seconds(), reason="Try a new route: " + reason)
        self._save()
        return _decision("active", False, None, "wait", reason, "")

    def render_completion_history(self) -> str:
        labels = {"complete": "Completed", "complete_refused": "Completion refused",
                  "hard_limit_breach": "Hard limit breach"}
        rows = [row for row in (self.state.revisions if self.state else [])
                if row.get("kind") in labels]
        return "\n".join(f"{labels[row['kind']]}: {row['reason']}" for row in rows[-5:])
