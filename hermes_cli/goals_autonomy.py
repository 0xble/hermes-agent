"""Opt-in autonomous transitions on the native goal persistence boundary."""

from __future__ import annotations

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


def goal_autonomy_enabled() -> bool:
    # Resolve at call time under the owning profile, including idle wake scans.
    from hermes_cli.config_effective import load_user_config_effective

    return (load_user_config_effective().get("goals") or {}).get("autonomy") is True


def completion_criteria(state) -> list[str]:
    from hermes_cli.goals import contract_items

    items = [*contract_items("verification", state.contract.verification),
             *contract_items("subgoals", state.subgoals)]
    return list(dict.fromkeys(items or [state.contract.outcome or state.goal]))


def record_hard_limit_breach(state, reason: str) -> None:
    breached = re.search(r"(?i)(?:constraint|hard.limit).{0,40}(?:breach|violat)|"
                         r"(?:breach|violat).{0,40}(?:constraint|hard.limit)", reason or "")
    if breached and not any(
            row.get("kind") == "hard_limit_breach" and row.get("reason") == reason
            for row in state.revisions):
        state.revisions.append({"at": time.time(), "kind": "hard_limit_breach", "reason": reason,
                                "actor": "system", "authority": "autonomy"})


def migrate_autonomous_goal(state) -> bool:
    if not goal_autonomy_enabled():
        return False
    system_pause = state.status in {"blocked", "needs-input", "awaiting-approval"} or (
        state.status == "paused" and (state.last_verdict == "blocked" or
        (state.paused_reason or "").startswith(("turn budget exhausted", "quality gate", "judge "))))
    if not system_pause and not state.waiting_on_user_message:
        return False
    reason = state.last_reason or state.paused_reason or "legacy input wait"
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


def autonomous_edit(manager, *, reason, goal=None, contract=None, subgoals=None,
                    max_turns=None, user_quote="", kind="revise"):
    """Validate the entire delta before changing the staged snapshot."""
    from hermes_cli.goals import GoalContract, contract_items
    from hermes_cli.goals_authority import (
        _normalize_authority_value, _removal_instruction_units,
    )
    from hermes_cli.session_controls import check_user_quote

    state = manager.state
    if state is None or state.status not in {"active", "paused"}:
        return {"ok": False, "error_code": "no_goal", "error": "no active or paused goal"}
    if not str(reason or "").strip():
        return {"ok": False, "error_code": "reason_required", "error": "a revision needs a reason"}
    before = {"goal": state.goal, **state.contract.to_dict(), "subgoals": list(state.subgoals),
              "max_turns": state.max_turns}
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
        checked = check_user_quote(manager.session_id, user_quote, cursor=manager._cursor) if user_quote else "missing"
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
    # Keep created_at: already collected proof and recorded breaches still bind this goal.
    revision = {"at": time.time(), "actor": "agent", "authority": "autonomy", "kind": kind,
                "reason": reason, "before": before, "after": after,
                "user_quote": user_quote if removed else ""}
    state.revisions.append(revision)
    manager._save()
    return {"ok": True, "revision": revision, "state": state, "version": len(state.revisions) + 1}


class GoalAutonomyMixin:
    def complete(self, *, reason: str, evidence: Mapping[str, str]):
        from hermes_cli.goals_revisions import revise_goal_snapshot

        if not goal_autonomy_enabled():
            return {"ok": False, "error_code": "autonomy_disabled", "error": "completion is owned by the judge"}
        if not self.has_goal():
            return {"ok": False, "error_code": "no_goal", "error": "no active or paused goal"}
        return revise_goal_snapshot(self, reason=reason, evidence=evidence, _complete=True)

    def _complete(self, *, reason, evidence):
        state = self._require_goal()
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
        self.wait_for_seconds(900, reason="Try a new route: " + reason)
        self._save()
        return _decision("active", False, None, "wait", reason, "")

    def render_completion_history(self) -> str:
        rows = [row for row in (self.state.revisions if self.state else [])
                if row.get("kind") in {"complete", "complete_refused", "hard_limit_breach"}]
        return "\n".join(f"{row['kind']}: {row['reason']}" for row in rows[-5:])
