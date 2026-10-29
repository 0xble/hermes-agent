"""Goal contract commands and quality gates; persistence stays on GoalManager.

Facade dependencies are late-bound so evaluator and command test seams remain shared.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from hermes_cli.lifecycle_snapshot import snapshot_control

logger = logging.getLogger("hermes_cli.goals")

if TYPE_CHECKING:
    from hermes_cli.goals import GoalContract, GoalGate, GoalState


def _recorded_result_text(content: str) -> str:
    """Decode one JSON envelope without joining fields or dropping negative context."""
    try:
        json.loads(content)
    except (json.JSONDecodeError, RecursionError):
        return content
    # Preserve field separators and all metadata. Only undo serialization escaping,
    # once, so literal backslashes and excerpts spanning distinct fields stay distinct.
    return re.sub(
        r'"(?:[^"\\]|\\.)*"',
        lambda match: '"' + json.loads(match.group(0)) + '"',
        content,
    )


class GoalContractCommandsMixin:
    # --- revisions ------------------------------------------------------
    # Public revise()/replace() live on GoalRevisionsMixin (snapshot/CAS boundary); this mixin
    # supplies the pure additive-revision validator and the recorded-evidence check they use.

    def _recorded_evidence_error(self, evidence: str, state: GoalState) -> Optional[Dict[str, str]]:
        """Require evidence-backed revisions to cite an eligible recorded tool result.

        Matching uses word-bounded decoded or identically whitespace-normalized token sequences.
        This accepts a multiline excerpt copied from a result without accepting partial tokens or prose
        fabricated by the agent: every candidate still comes from a recorded tool row in this session.
        """
        from hermes_cli.goals import (
            _bounded_evidence_match, _evidence_is_negated, _evidence_tool_excluded, _get_session_db,
            goal_autonomy_enabled,
        )

        db = _get_session_db()
        if db is None:
            return {"error_code": "evidence_not_recorded",
                    "error": "evidence must appear in a recorded tool result"}
        raw_evidence = str(evidence or "")
        normalized_evidence = " ".join(raw_evidence.split())
        if not normalized_evidence:
            return {"error_code": "evidence_not_recorded",
                    "error": "evidence must appear in a recorded tool result"}
        try:
            # get_messages() returns tool_name provenance (messages_by_role() does not), so the
            # bookkeeping exclusion actually applies. include_inactive keeps archived results.
            reader = getattr(db, "get_messages", None)
            if reader is not None:
                rows = [row for row in reader(self.session_id, include_inactive=True)
                        if row.get("role") == "tool"
                        and float(row.get("timestamp") or 0.0) >= float(state.created_at or 0.0)]
                found_negated = False
                for row in rows:
                    if _evidence_tool_excluded(str(row.get("tool_name") or "")):
                        continue
                    raw_content = str(row.get("content") or "")
                    content = _recorded_result_text(raw_content)
                    if _bounded_evidence_match(raw_evidence, content):
                        # Decoded punctuation must not remove an existing envelope veto.
                        if any(_evidence_is_negated(candidate, raw_evidence, include_no=goal_autonomy_enabled())
                               for candidate in (raw_content, content)):
                            found_negated = True
                            continue
                        return None
                if found_negated:
                    return {"error_code": "evidence_negated",
                            "error": "the recorded evidence is negated or preserves the earlier requirement"}
        except Exception as exc:
            logger.debug("goal revise: evidence lookup failed: %s", exc, exc_info=True)
        return {"error_code": "evidence_not_recorded",
                "error": "evidence must appear in a recorded tool result from this goal's session"}

    @staticmethod
    def _revise_state(state: GoalState, *, reason: str, actor: str, goal: Optional[str],
                      contract: Optional[Dict[str, str]], subgoals: Optional[List[str]],
                      evidence: str, evidence_error: Optional[Dict[str, str]]) -> Dict[str, Any]:
        """Validate and apply an additive revision to the live row (pure; no I/O)."""
        from hermes_cli.goals import (
            GoalContract, _CONTRACT_FIELDS, _REVISION_EVIDENCE_MIN_CHARS, _requirement_diff,
            contract_items, goal_remove_usage, time,
        )

        before = {"goal": state.goal, **state.contract.to_dict(),
                  "subgoals": contract_items("subgoals", state.subgoals)}
        after = dict(before)
        if goal is not None and goal.strip():
            after["goal"] = goal.strip()
        for key, value in (contract or {}).items():
            if key not in _CONTRACT_FIELDS:
                return {"ok": False, "error_code": "unknown_field", "error": f"unknown contract field: {key}"}
            after[key] = str(value or "").strip()
        if subgoals is not None:
            after["subgoals"] = contract_items("subgoals", subgoals)
        changed = [key for key in after if after[key] != before[key]]
        if not changed:
            return {"ok": False, "error_code": "no_change", "error": "the revision changes nothing"}

        if "goal" in changed:
            return {
                "ok": False, "error_code": "destructive_change_requires_command",
                "error": f"Destructive goal changes require /goal replace <text> or {goal_remove_usage()}. Goal unchanged and still running.",
            }

        item_diffs = {key: _requirement_diff(key, before[key], after[key]) for key in changed}
        destructive = {key: diff[1] for key, diff in item_diffs.items() if diff[1] and key != "verification"}
        if destructive:
            return {
                "ok": False, "error_code": "destructive_change_requires_command",
                "error": f"Destructive goal changes require /goal replace <text> or {goal_remove_usage()}. Goal unchanged and still running.",
            }

        if evidence and len(" ".join(evidence.split())) < _REVISION_EVIDENCE_MIN_CHARS:
            return {"ok": False, "error_code": "evidence_too_short",
                    "error": f"evidence must be at least {_REVISION_EVIDENCE_MIN_CHARS} characters"}
        verification_diff = item_diffs.get("verification", ([], [], []))
        verification_removed = verification_diff[1]
        if evidence and set(changed) != {"verification"}:
            return {"ok": False, "error_code": "evidence_not_authorized",
                    "error": "evidence may authorize only verification changes; the goal remains unchanged and running"}
        if verification_removed and not evidence:
            return {
                "ok": False, "error_code": "destructive_change_requires_command",
                "error": f"Destructive goal changes require /goal replace <text> or {goal_remove_usage()}. Goal unchanged and still running.",
            }
        if evidence:
            if evidence_error:
                return {"ok": False, **evidence_error}
            if verification_removed:
                return {
                    "ok": False, "error_code": "evidence_cannot_retire_verification",
                    "error": "recorded evidence has no per-check retirement disposition; "
                             "retain every existing verification check or ask the user to use "
                             "/goal remove check <n>. Goal unchanged and still running.",
                }

        authority = "evidence" if evidence else "agent_addition"
        revision = {
            "at": time.time(), "actor": actor, "reason": reason,
            "authority": authority, "evidence": evidence,
            "before": {key: before[key] for key in changed},
            "after": {key: after[key] for key in changed},
        }
        state.goal = after["goal"]
        state.contract = GoalContract.from_dict({key: after[key] for key in _CONTRACT_FIELDS})
        state.subgoals = contract_items("subgoals", after["subgoals"])
        state.revisions.append(revision)
        return {"ok": True, "revision": revision,
                "version": sum(1 for item in state.revisions if item.get("kind") != "replace") + 1}

    # --- /subgoal user controls ---------------------------------------

    @snapshot_control
    def add_subgoal(self, text: str) -> str:
        """Append a user-added criterion; raises ``RuntimeError`` without ``has_goal()``."""
        from hermes_cli.goals import (
            _live_goal, contract_items,
        )

        self._require_goal()
        text = (text or "").strip()
        if not text:
            raise ValueError("subgoal text is empty")

        def _apply(state):
            state = _live_goal(state)
            state.subgoals = contract_items("subgoals", [*state.subgoals, text])
            return text
        return self._mutate_current(_apply)

    def _pop_item(self, attr: str, index_1based: int):
        from hermes_cli.goals import (
            _live_goal,
        )

        self._require_goal()

        def _apply(state):
            items = getattr(_live_goal(state), attr)
            idx = int(index_1based) - 1
            if idx < 0 or idx >= len(items):
                raise IndexError(f"index out of range (1..{len(items)})")
            return items.pop(idx)
        return self._mutate_current(_apply)

    def _clear_items(self, attr: str) -> int:
        from hermes_cli.goals import (
            _live_goal,
        )

        self._require_goal()

        def _apply(state):
            state = _live_goal(state)
            prev = len(getattr(state, attr))
            setattr(state, attr, [])
            return prev
        return self._mutate_current(_apply)

    @staticmethod
    def _record_explicit_removal(state: GoalState, field: str, before: Any, after: Any) -> None:
        """Log the current-contract edit; the audit row never binds future requirements."""
        from hermes_cli.goals import (
            time,
        )

        state.revisions.append({
            "at": time.time(), "actor": "user", "kind": "remove", "authority": "explicit_command",
            "reason": f"explicit /goal remove {field} command",
            "before": {field: before}, "after": {field: after},
        })

    @snapshot_control
    def remove_subgoal(self, index_1based: int) -> str:
        """Remove one current subgoal by 1-based index, against the live durable row."""
        from hermes_cli.goals import (
            _live_goal, contract_items,
        )

        self._require_goal()

        def _apply(state):
            state = _live_goal(state)
            items = contract_items("subgoals", state.subgoals)
            idx = int(index_1based) - 1
            if idx < 0 or idx >= len(items):
                raise IndexError(f"index out of range (1..{len(items)})")
            before = list(items)
            removed = items.pop(idx)
            state.subgoals = contract_items("subgoals", items)
            self._record_explicit_removal(state, "subgoals", before, list(state.subgoals))
            return removed
        return self._mutate_current(_apply)

    @snapshot_control
    def remove_contract_item(self, field: str, index_1based: int) -> str:
        """Remove one numbered contract item through an explicit user command."""
        from hermes_cli.goals import (
            _live_goal, contract_items, contract_remove_aliases, goal_remove_usage,
        )

        self._require_goal()
        key = contract_remove_aliases().get(field.lower())
        if key is None:
            raise ValueError(f"Usage: {goal_remove_usage()}")

        def _apply(state):
            state = _live_goal(state)
            items = contract_items(key, getattr(state.contract, key))
            idx = int(index_1based) - 1
            if idx < 0 or idx >= len(items):
                raise IndexError(f"index out of range (1..{len(items)})")
            before = list(items)
            removed = items.pop(idx)
            setattr(state.contract, key, "\n".join(items))
            self._record_explicit_removal(state, key, before, list(items))
            return removed
        return self._mutate_current(_apply)

    def render_removable_items(self) -> str:
        """Render numbered items discoverable by ``/goal remove``."""
        from hermes_cli.goals import (
            render_removable_contract_items,
        )

        return render_removable_contract_items(self._state)

    @snapshot_control
    def clear_subgoals(self) -> int:
        """Wipe all subgoals. Returns the previous count."""
        return self._clear_items("subgoals")

    def render_subgoals(self) -> str:
        """Public helper for the /subgoal slash command."""
        if self._state is None:
            return "(no active goal)"
        return self._state.render_subgoals_block() or "(no subgoals — use /subgoal <text> to add criteria)"

    # --- /goal gate quality gates ---------------------------------------

    @snapshot_control
    def add_gate(self, command: str, *, timeout_seconds: Optional[int] = None, max_retries: Optional[int] = None) -> GoalGate:
        """Append a quality-gate command; raises ``RuntimeError`` without ``has_goal()``."""
        from hermes_cli.goals import (
            DEFAULT_GATE_MAX_RETRIES, DEFAULT_GATE_TIMEOUT_SECONDS, GoalGate, _live_goal,
            asdict,
        )

        self._require_goal()
        command = (command or "").strip()
        if not command:
            raise ValueError("gate command is empty")
        gate = GoalGate(
            command=command,
            timeout_seconds=int(timeout_seconds) if timeout_seconds else DEFAULT_GATE_TIMEOUT_SECONDS,
            max_retries=int(max_retries) if max_retries else DEFAULT_GATE_MAX_RETRIES,
        )

        def _apply(state):
            _live_goal(state).gates.append(GoalGate(**asdict(gate)))
            return gate
        return self._mutate_current(_apply)

    @snapshot_control
    def remove_gate(self, index_1based: int) -> str:
        """Remove a gate by 1-based index. Returns the removed command."""
        return self._pop_item("gates", index_1based).command

    @snapshot_control
    def clear_gates(self) -> int:
        """Remove all gates. Returns the previous count."""
        return self._clear_items("gates")

    def render_gates(self) -> str:
        """Public helper for the /goal gate slash command."""
        if self._state is None:
            return "(no active goal)"
        if not self._state.gates:
            return "(no quality gates — use /goal gate add <command> to require one)"
        lines = []
        for i, g in enumerate(self._state.gates, start=1):
            status = ""
            if g.last_exit_code == 0:
                status = " ✓ passing"
            elif g.last_exit_code is not None:
                status = f" ✗ failing (exit {g.last_exit_code}, attempt {g.attempts}/{g.max_retries})"
            lines.append(f"- {i}. $ {g.command}{status}")
        return "\n".join(lines)

    def _check_gates(self) -> Optional[Dict[str, Any]]:
        """Run quality gates in order; return a decision dict on failure.

        Every eligible boundary re-executes a failed gate. A git HEAD+porcelain fingerprint used to
        replay the recorded failure when "nothing changed", but porcelain sees neither the contents
        of an untracked or already-modified file nor inputs outside the repo, so a repaired input
        was replayed as still-failing until retry exhaustion paused the goal (#110649). The
        retry cap below still bounds a genuinely stuck red suite.
        """
        from hermes_cli.goals import (
            _decision, _gate_workspace, _goal_budget_label,
            run_gate,
        )

        state = self._state
        if state is None or not state.gates:
            return None

        gate_cwd, refusal = _gate_workspace()
        if refusal:
            return self._pause_decision(
                f"quality gates not run: {refusal}", "gate_failed", f"gates not run: {refusal}",
                f"⏸ Goal paused — quality gates not run: {refusal}. Fix the workspace or "
                f"/goal gate remove the gates, then /goal resume.",
            )
        for gate in state.gates:
            passed, exit_code, tail = run_gate(gate, cwd=gate_cwd)
            gate.last_exit_code = exit_code
            gate.last_output_tail = tail
            if passed:
                gate.attempts = 0
                continue

            gate.attempts += 1

            if gate.attempts > gate.max_retries:
                return self._pause_decision(
                    f"quality gate exhausted {gate.attempts - 1} retries: $ {gate.command}",
                    "gate_failed", f"gate exhausted retries: $ {gate.command}",
                    f"⏸ Goal paused — quality gate still failing after "
                    f"{gate.max_retries} retries: $ {gate.command} "
                    f"(exit {exit_code}). Fix it manually or /goal gate remove it, "
                    f"then /goal resume.",
                )

            self._save()
            prompt = self._gate_failure_prompt(gate, exit_code, tail)
            return _decision(
                "active", True, prompt, "gate_failed",
                f"gate failed (exit {exit_code}): $ {gate.command}",
                f"✗ Quality gate failed ({_goal_budget_label(state.turns_used, state.max_turns)} turns, "
                f"attempt {gate.attempts}/{gate.max_retries}): $ {gate.command}",
            )

        self._save()
        return None
