"""Candidate-only goal lifecycle plugin.

This plugin exposes only additive model actions. User-only controls such as
pause, resume, clear, and replacement remain in Hermes slash commands.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from hermes_cli.goals import DEFAULT_MAX_TURNS, GoalContract, GoalManager, load_goal

_SCHEMA = {
    "name": "goal_set",
    "description": "Enroll or inspect the current standing goal for this session. Use action=set for a substantial authorized task, status to inspect it, and subgoal_add only to add acceptance criteria. Pause, resume, clear, and replacement are user-only controls.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["set", "status", "subgoal_add"]},
            "goal": {"type": "string", "description": "The durable objective to pursue."},
            "verification": {"type": "string", "description": "Evidence required before completion."},
            "constraints": {"type": "string", "description": "Constraints that must remain true."},
            "boundaries": {"type": "string", "description": "Scope boundaries for the work."},
            "stop_when": {"type": "string", "description": "Condition that ends the work."},
            "text": {"type": "string", "description": "An additive acceptance criterion."},
        },
        "required": ["action"],
    },
}

def _result(**fields: Any) -> str:
    return json.dumps(fields, sort_keys=True)


def _session_id(kwargs: Mapping[str, Any]) -> str:
    # task_id is the trusted registry scope. session_id is accepted for direct
    # plugin tests and runtimes that expose both names.
    return str(kwargs.get("session_id") or kwargs.get("task_id") or "").strip()


def _state_payload(manager: GoalManager) -> dict[str, Any] | None:
    state = manager.state
    return json.loads(state.to_json()) if state is not None else None


def _configured_max_turns() -> int:
    """Use the active profile's policy, never model-supplied tool arguments."""
    from hermes_cli.config import load_config

    try:
        value = int(((load_config() or {}).get("goals") or {}).get("max_turns") or DEFAULT_MAX_TURNS)
        return value if value > 0 else DEFAULT_MAX_TURNS
    except (TypeError, ValueError, AttributeError):
        return DEFAULT_MAX_TURNS


def format_goal_notice(action: str, state: Any, *, subgoal: str | None = None) -> str:
    """Human receipt for a committed agent mutation, rendered from the persisted read-back
    state (never from the tool args) so the user sees exactly what was stored."""
    if action == "set":
        text = f"⊙ Goal set ({state.max_turns}-turn budget): {state.goal}"
        if state.has_contract():
            text += "\nCompletion contract:\n" + state.contract.render_block()
        return text
    if action == "subgoal_add":
        index = len(state.subgoals)
        return f"✓ Subgoal added ({index}): {subgoal}\nGoal: {state.goal}"
    return ""


def goal_set(args: dict[str, Any], **kwargs: Any) -> str:
    """Handle the restricted goal lifecycle surface without raising."""
    try:
        from agent.delegation_context import is_delegated_child_context

        if is_delegated_child_context():
            return _result(success=False, error_code="parent_only",
                           error="goal_set is available only to the owning parent session")
        action = str(args.get("action") or "status").strip().lower()
        if action not in {"set", "status", "subgoal_add"}:
            return _result(success=False, error_code="user_control_only",
                           error="pause, resume, clear, and replacement remain user-only controls")
        sid = _session_id(kwargs)
        if not sid:
            return _result(success=False, error_code="missing_session_scope",
                           error="goal_set requires trusted active session scope")
        manager = GoalManager(sid)
        if action == "status":
            return _result(success=True, action=action, state=_state_payload(manager))
        if action == "set":
            goal = str(args.get("goal") or "").strip()
            if not goal:
                return _result(success=False, error_code="invalid_goal", error="goal is required")
            if manager.has_goal():
                return _result(success=False, error_code="active_goal_exists",
                               error="an active or paused goal already exists; use the user goal controls")
            contract_data = {
                key: str(args.get(key) or "").strip()
                for key in ("verification", "constraints", "boundaries", "stop_when")
                if str(args.get(key) or "").strip()
            }
            state = manager.set(goal, max_turns=_configured_max_turns(),
                                contract=GoalContract.from_dict(contract_data))
            persisted = load_goal(sid)
            if persisted is None or persisted.to_json() != state.to_json():
                return _result(success=False, error_code="goal_persistence_failed",
                               error="goal read-back did not match")
            return _result(success=True, action=action, persisted=True, state=_state_payload(manager),
                           notice=format_goal_notice(action, persisted))
        text = str(args.get("text") or "").strip()
        if not text:
            return _result(success=False, error_code="invalid_subgoal", error="text is required")
        if not manager.has_goal():
            return _result(success=False, error_code="no_active_goal", error="no active goal")
        manager.add_subgoal(text)
        persisted = load_goal(sid)
        if persisted is None or text not in persisted.subgoals:
            return _result(success=False, error_code="goal_persistence_failed",
                           error="subgoal read-back did not match")
        return _result(success=True, action=action, persisted=True, state=_state_payload(manager),
                       notice=format_goal_notice(action, persisted, subgoal=text))
    except Exception as exc:
        return _result(success=False, error_code="goal_tool_error", error=str(exc))


def register(ctx: Any) -> None:
    ctx.register_tool(name="goal_set", toolset="goal_lifecycle",
                      schema=_SCHEMA, handler=goal_set)
