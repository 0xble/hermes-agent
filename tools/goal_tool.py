#!/usr/bin/env python3
"""Model-callable control of the existing per-session goal loop."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Optional

from tools.registry import registry, tool_error, tool_result

READ_ACTIONS = frozenset({"status", "show", "subgoal_list", "gate_list"})
MUTATION_ACTIONS = frozenset({
    "set",
    "draft",
    "pause",
    "resume",
    "clear",
    "wait",
    "unwait",
    "subgoal_add",
    "subgoal_remove",
    "subgoal_clear",
    "gate_add",
    "gate_remove",
    "gate_clear",
})
GOAL_ACTIONS = tuple(sorted(READ_ACTIONS | MUTATION_ACTIONS))

_ACTIVATION_RE = re.compile(
    r"(?:\b(?:set|create|start|activate|establish|make|replace|overwrite|supersede|switch|change)\b.{0,80}\b(?:standing\s+|active\s+|current\s+)?goal\b"
    r"|\b(?:standing\s+|active\s+|current\s+)?goal\b.{0,80}\b(?:set|create|start|activate|establish|make|replace|overwrite|supersede|switch|change)\b)",
    re.IGNORECASE | re.DOTALL,
)
_NEGATED_ACTIVATION_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never|without)\b.{0,48}"
    r"\b(?:set|create|start|activate|establish|make|replace|overwrite|"
    r"supersede|switch|change|pause|resume|clear|park|add|remove)\b",
    re.IGNORECASE | re.DOTALL,
)
_NEGATED_MUTATION_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never|not)\b[^.!?\n]{0,80}"
    r"\b(?:set|draft|start|activate|replace|pause|resume|clear|park|wait|unwait|add|remove)\b",
    re.IGNORECASE,
)
_REPLACEMENT_RE = re.compile(
    r"(?:\b(?:replace|overwrite|supersede|switch|change)\b.{0,80}\b(?:standing\s+|active\s+|current\s+)?goal\b"
    r"|\b(?:standing\s+|active\s+|current\s+)?goal\b.{0,80}\b(?:replace|overwrite|supersede|switch|change)\b)",
    re.IGNORECASE | re.DOTALL,
)
_NON_DIRECT_CONTEXT_RE = re.compile(
    r"(?:\b(?:recommend|assess|evaluate|consider|decide|explain|discuss|suggest)\b"
    r".{0,80}\b(?:whether|if)\b.{0,40}$"
    r"|\bshould\s+(?:i|we|you|this|that|it)\b.{0,40}$)",
    re.IGNORECASE | re.DOTALL,
)
_ACTION_AUTH_RE = {
    "pause": re.compile(
        r"(?:\bpause\b.{0,80}\bgoal\b|\bgoal\b.{0,80}\bpause\b)", re.I | re.S
    ),
    "resume": re.compile(
        r"(?:\bresume\b.{0,80}\bgoal\b|\bgoal\b.{0,80}\bresume\b)", re.I | re.S
    ),
    "clear": re.compile(
        r"\b(?:clear|drop|remove|stop)\b\s+"
        r"(?:(?:the|my|this|our|your)\s+)?"
        r"(?:(?:standing|active|current|completed|done|paused)\s+)?"
        r"goal\b(?!(?:['’]s)?\s+(?:wait|barrier|subgoals?|(?:quality\s+)?gates?)\b)",
        re.I | re.S,
    ),
    "wait": re.compile(
        r"(?:\b(?:wait|park)\b.{0,80}\bgoal\b|\bgoal\b.{0,80}\b(?:wait|park)\b)",
        re.I | re.S,
    ),
    "unwait": re.compile(
        r"(?:\b(?:clear|remove|drop|release)\b.{0,80}\b(?:goal\s+)?wait(?:\s+barrier)?\b|\bunwait\b)",
        re.I | re.S,
    ),
    "subgoal_add": re.compile(
        r"\b(?:add|create|append)\b.{0,80}\bsubgoal\b", re.I | re.S
    ),
    "subgoal_remove": re.compile(
        r"\b(?:remove|delete|drop)\b.{0,80}\bsubgoal\b", re.I | re.S
    ),
    "subgoal_clear": re.compile(r"\bclear\b.{0,80}\bsubgoals?\b", re.I | re.S),
    "gate_add": re.compile(
        r"\b(?:add|create|append)\b.{0,80}\b(?:quality\s+)?gate\b", re.I | re.S
    ),
    "gate_remove": re.compile(
        r"\b(?:remove|delete|drop)\b.{0,80}\b(?:quality\s+)?gate\b", re.I | re.S
    ),
    "gate_clear": re.compile(r"\bclear\b.{0,80}\b(?:quality\s+)?gates?\b", re.I | re.S),
}
_DRAFT_RE = re.compile(
    r"\bdraft(?:\s+and\s+(?:set|start|activate|create))?\s+(?:a\s+|the\s+)?goal\b",
    re.I | re.S,
)
_NON_DIRECT_MUTATION_RE = re.compile(
    r"\b(?:explain|explanation|example|describe|discuss|teach|show|tell|consider|assess|evaluate|recommend|suggest)\b"
    r".{0,80}\b(?:how|why|whether|if|should|ways?|options?)\b",
    re.I | re.S,
)
_LATER_REVOCATION_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never)\b[^.!?\n]{0,80}"
    r"\b(?:set|draft|start|activate|replace|pause|resume|clear|park|wait|unwait|add|remove|do)\b"
    r"|\b(?:actually\s*,?\s*)?(?:do\s+not|don't|dont|never)\s*[.!?]*(?:\s|$)"
    r"|\b(?:never\s+mind|scratch\s+that|cancel\s+that|ignore\s+that)\b",
    re.I,
)


def _failure(error_code: str, message: str, **fields: Any) -> str:
    return tool_error(message, success=False, error_code=error_code, **fields)


def _explicit_activation_requested(
    text: str, prefix: str = "", context: Optional[str] = None
) -> bool:
    return bool(
        text.strip()
        and _ACTIVATION_RE.search(text)
        and not _NEGATED_ACTIVATION_RE.search(context or text)
        and not _NON_DIRECT_CONTEXT_RE.search(prefix)
    )


def _explicit_replacement_requested(text: str, context: Optional[str] = None) -> bool:
    return bool(
        text.strip()
        and _REPLACEMENT_RE.search(text)
        and not _NEGATED_ACTIVATION_RE.search(context or text)
    )


def _authorization_sentence(user_task: str, span_start: int, span_length: int) -> str:
    sentence_start = (
        max(user_task.rfind(separator, 0, span_start) for separator in ".!?\n") + 1
    )
    after_span = span_start + span_length
    sentence_ends = [
        position
        for separator in ".!?\n"
        if (position := user_task.find(separator, after_span)) >= 0
    ]
    sentence_end = min(sentence_ends) + 1 if sentence_ends else len(user_task)
    return user_task[sentence_start:sentence_end]


def _normalize_positive_int(
    value: Any, field: str, *, optional: bool = False
) -> Optional[int]:
    if value is None:
        if optional:
            return None
        raise ValueError(f"{field} must be a positive integer")
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a positive integer") from None
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{field} must be a positive integer")
    if number <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return number


def _normalize_max_turns(max_turns: Optional[int]) -> Optional[int]:
    return _normalize_positive_int(max_turns, "max_turns", optional=True)


def _resolve_default_max_turns(default_max_turns: Optional[int] = None) -> int:
    from hermes_cli.goals import DEFAULT_MAX_TURNS

    if default_max_turns is not None:
        value = _normalize_max_turns(default_max_turns)
        return int(value or DEFAULT_MAX_TURNS)
    try:
        from hermes_cli.config import load_config

        goals_cfg = (load_config() or {}).get("goals") or {}
        return int(goals_cfg.get("max_turns", DEFAULT_MAX_TURNS) or DEFAULT_MAX_TURNS)
    except Exception:
        return DEFAULT_MAX_TURNS


def _state_payload(state: Any) -> Optional[dict[str, Any]]:
    if state is None:
        return None
    return json.loads(state.to_json())


def _authorized_action(
    action: str,
    *,
    user_task: Optional[str],
    authorization_text: Optional[str],
) -> tuple[bool, str, str]:
    task_text = user_task if isinstance(user_task, str) else ""
    auth_text = authorization_text if isinstance(authorization_text, str) else ""
    if not task_text.strip() or not auth_text.strip():
        return False, "", ""
    auth_start = task_text.find(auth_text)
    if auth_start < 0:
        return False, "authorization_not_in_current_turn", ""
    context = _authorization_sentence(task_text, auth_start, len(auth_text))
    if _NEGATED_ACTIVATION_RE.search(context) or _NEGATED_MUTATION_RE.search(context):
        return False, "", context
    prefix = task_text[:auth_start]
    suffix = task_text[auth_start + len(auth_text) :]
    if _NON_DIRECT_MUTATION_RE.search(context) or _NON_DIRECT_MUTATION_RE.search(
        prefix[-160:]
    ):
        return False, "", context
    if _LATER_REVOCATION_RE.search(suffix):
        return False, "", context
    if action == "draft":
        ok = bool(_DRAFT_RE.search(auth_text) and _ACTIVATION_RE.search(auth_text))
    elif action == "set":
        ok = _explicit_activation_requested(auth_text, prefix, context)
    else:
        pattern = _ACTION_AUTH_RE.get(action)
        ok = bool(pattern and pattern.search(auth_text))
    return ok, "", context


def _success(
    action: str, *, state: Any, change: Optional[dict[str, Any]] = None, **fields: Any
) -> str:
    return tool_result(
        success=True,
        persisted=True,
        action=action,
        state=_state_payload(state),
        change=change or {},
        **fields,
    )


def set_goal_tool(
    goal: str = "",
    *,
    action: str = "set",
    session_id: str,
    user_task: Optional[str] = None,
    authorization_text: Optional[str] = None,
    max_turns: Optional[int] = None,
    default_max_turns: Optional[int] = None,
    contract: Optional[Mapping[str, Any]] = None,
    replace_existing: bool = False,
    pid: Optional[int] = None,
    reason: str = "",
    text: str = "",
    index: Optional[int] = None,
    command: str = "",
    timeout_seconds: Optional[int] = None,
    max_retries: Optional[int] = None,
    turn_id: Optional[str] = None,
    goal_control_revision: Optional[int] = None,
) -> str:
    """Inspect or mutate the current session's standing goal."""
    normalized_action = str(action or "set").strip().lower()
    if normalized_action not in READ_ACTIONS | MUTATION_ACTIONS:
        return _failure(
            "invalid_action",
            f"unsupported goal action: {normalized_action or '<empty>'}",
        )
    sid = session_id.strip() if isinstance(session_id, str) else ""
    if not sid:
        return _failure(
            "missing_session_scope", "set_goal requires trusted active session scope"
        )

    try:
        from hermes_cli.goals import (
            GoalContract,
            GoalManager,
            guard_goal_activation,
            load_goal,
        )

        default_turns = _resolve_default_max_turns(default_max_turns)
        manager = GoalManager(session_id=sid, default_max_turns=default_turns)

        if normalized_action in READ_ACTIONS:
            state = manager.refresh()
            if normalized_action == "subgoal_list":
                return _success(
                    normalized_action,
                    state=state,
                    items=list(state.subgoals) if state else [],
                )
            if normalized_action == "gate_list":
                return _success(
                    normalized_action,
                    state=state,
                    items=[gate.to_dict() for gate in state.gates] if state else [],
                )
            return _success(normalized_action, state=state)

        turn = str(turn_id or "").strip()
        if not turn:
            return _failure(
                "missing_turn_scope",
                "set_goal mutations require trusted active turn scope",
            )
        if goal_control_revision is None:
            return _failure(
                "missing_goal_control_revision",
                "set_goal mutations require goal-control authority captured at turn start",
            )
        if (
            isinstance(goal_control_revision, bool)
            or not isinstance(goal_control_revision, int)
            or goal_control_revision < 0
        ):
            return _failure(
                "invalid_goal_control_revision",
                "goal-control revision must be a non-negative integer",
            )

        authorized, auth_error, auth_context = _authorized_action(
            normalized_action,
            user_task=user_task,
            authorization_text=authorization_text,
        )
        if auth_error:
            return _failure(
                auth_error,
                "authorization_text must be an exact span from the current user turn",
            )
        if not authorized:
            return _failure(
                "explicit_goal_authorization_required",
                f"The current user turn must explicitly authorize goal action {normalized_action!r}",
            )

        with guard_goal_activation(sid, goal_control_revision) as current:
            if not current:
                return _failure(
                    "goal_activation_cancelled",
                    "A newer user goal-control command cancelled this action from the running turn",
                )
            state = manager.refresh()
            change: dict[str, Any] = {}

            if normalized_action in {"set", "draft"}:
                if not isinstance(goal, str) or not goal.strip():
                    return _failure("invalid_goal", "goal text is empty")
                if contract is not None and not isinstance(contract, Mapping):
                    return _failure("invalid_contract", "contract must be an object")
                goal_contract = GoalContract.from_dict(dict(contract or {}))
                if normalized_action == "draft" and goal_contract.is_empty():
                    return _failure(
                        "invalid_contract",
                        "draft requires a concrete completion contract with verification, constraints, boundaries, or stop_when",
                    )
                try:
                    turns = _normalize_max_turns(max_turns)
                except ValueError as exc:
                    return _failure("invalid_max_turns", str(exc))
                if turns is not None and turns > default_turns:
                    return _failure(
                        "turn_budget_exceeded",
                        f"max_turns ({turns}) exceeds configured goal budget ({default_turns})",
                    )
                has_existing = bool(state and state.status in {"active", "paused"})
                existing_goal = state.goal if state else ""
                if has_existing and not replace_existing:
                    return _failure(
                        "active_goal_exists",
                        "An active or paused goal already exists. Ask explicitly to replace it, then set replace_existing=true.",
                        existing_goal=existing_goal,
                    )
                auth_text = (
                    authorization_text if isinstance(authorization_text, str) else ""
                )
                if has_existing and not _explicit_replacement_requested(
                    auth_text, auth_context
                ):
                    return _failure(
                        "explicit_replacement_authorization_required",
                        "Replacing an active or paused goal requires explicit replacement language in the quoted authorization",
                        existing_goal=existing_goal,
                    )
                state = manager.set(
                    goal.strip(), max_turns=turns, contract=goal_contract
                )
                change = {
                    "kind": "goal_replaced" if has_existing else "goal_set",
                    "previous_goal": existing_goal if has_existing else None,
                }
                persisted = load_goal(sid)
                if persisted is None or persisted.to_json() != state.to_json():
                    return _failure(
                        "goal_persistence_failed",
                        "Goal activation was not confirmed by persistent readback",
                        persisted=False,
                    )
                return _success(
                    normalized_action,
                    state=state,
                    change=change,
                    status=state.status,
                    goal=state.goal,
                    max_turns=state.max_turns,
                    replaced_existing=has_existing,
                    replaced_goal=existing_goal if has_existing else None,
                    authorization_text=auth_text,
                    message="Goal set and active. Continue working toward it now.",
                )

            if state is None or state.status == "cleared":
                return _failure(
                    "no_active_goal", "No goal is available for this action"
                )
            if state.status == "done" and normalized_action != "clear":
                return _failure(
                    "invalid_goal_transition",
                    "A completed goal cannot be mutated or resumed",
                )

            expected_persisted_json: Optional[str] = None
            if normalized_action == "pause":
                if state.status != "active":
                    return _failure(
                        "invalid_goal_transition", "Only an active goal can be paused"
                    )
                state = manager.pause(reason=(reason or "agent-paused").strip())
                change = {"kind": "goal_paused"}
            elif normalized_action == "resume":
                if state.status != "paused":
                    return _failure(
                        "invalid_goal_transition", "Only a paused goal can be resumed"
                    )
                state = manager.resume(reset_budget=False)
                change = {"kind": "goal_resumed"}
            elif normalized_action == "clear":
                previous_goal = state.goal
                manager.clear()
                state = load_goal(sid)
                if (
                    state is None
                    or state.status != "cleared"
                    or state.goal != previous_goal
                ):
                    return _failure(
                        "goal_persistence_failed",
                        "Goal clear was not confirmed by persistent readback",
                    )
                change = {"kind": "goal_cleared", "previous_goal": previous_goal}
            elif normalized_action == "wait":
                try:
                    wait_pid = _normalize_positive_int(pid, "pid")
                except ValueError as exc:
                    return _failure("invalid_pid", str(exc))
                if wait_pid is None:
                    return _failure("invalid_pid", "pid must be a positive integer")
                state = manager.wait_on(wait_pid, reason=reason)
                change = {
                    "kind": "goal_parked",
                    "pid": wait_pid,
                    "reason": (reason or "").strip(),
                }
            elif normalized_action == "unwait":
                cleared = manager.stop_waiting()
                if manager.state is not None:
                    expected_persisted_json = manager.state.to_json()
                state = manager.refresh()
                change = {"kind": "goal_unparked", "cleared": cleared}
            elif normalized_action == "subgoal_add":
                added = manager.add_subgoal(text)
                if manager.state is not None:
                    expected_persisted_json = manager.state.to_json()
                state = manager.refresh()
                if state is None:
                    return _failure(
                        "goal_persistence_failed",
                        "Subgoal add was not confirmed by persistent readback",
                    )
                change = {
                    "kind": "subgoal_added",
                    "index": len(state.subgoals),
                    "text": added,
                }
            elif normalized_action == "subgoal_remove":
                try:
                    item_index = _normalize_positive_int(index, "index")
                    if item_index is None:
                        raise ValueError("index must be a positive integer")
                    removed = manager.remove_subgoal(item_index)
                    if manager.state is not None:
                        expected_persisted_json = manager.state.to_json()
                except (ValueError, IndexError, RuntimeError) as exc:
                    return _failure("invalid_subgoal", str(exc))
                state = manager.refresh()
                change = {
                    "kind": "subgoal_removed",
                    "index": item_index,
                    "text": removed,
                }
            elif normalized_action == "subgoal_clear":
                count = manager.clear_subgoals()
                if manager.state is not None:
                    expected_persisted_json = manager.state.to_json()
                state = manager.refresh()
                change = {"kind": "subgoals_cleared", "count": count}
            elif normalized_action == "gate_add":
                try:
                    timeout = _normalize_positive_int(
                        timeout_seconds, "timeout_seconds", optional=True
                    )
                    retries = _normalize_positive_int(
                        max_retries, "max_retries", optional=True
                    )
                    gate = manager.add_gate(
                        command, timeout_seconds=timeout, max_retries=retries
                    )
                    if manager.state is not None:
                        expected_persisted_json = manager.state.to_json()
                except (ValueError, RuntimeError) as exc:
                    return _failure("invalid_gate", str(exc))
                state = manager.refresh()
                if state is None:
                    return _failure(
                        "goal_persistence_failed",
                        "Gate add was not confirmed by persistent readback",
                    )
                change = {
                    "kind": "gate_added",
                    "index": len(state.gates),
                    **gate.to_dict(),
                }
            elif normalized_action == "gate_remove":
                try:
                    item_index = _normalize_positive_int(index, "index")
                    if item_index is None:
                        raise ValueError("index must be a positive integer")
                    removed = manager.remove_gate(item_index)
                    if manager.state is not None:
                        expected_persisted_json = manager.state.to_json()
                except (ValueError, IndexError, RuntimeError) as exc:
                    return _failure("invalid_gate", str(exc))
                state = manager.refresh()
                change = {
                    "kind": "gate_removed",
                    "index": item_index,
                    "command": removed,
                }
            elif normalized_action == "gate_clear":
                count = manager.clear_gates()
                if manager.state is not None:
                    expected_persisted_json = manager.state.to_json()
                state = manager.refresh()
                change = {"kind": "gates_cleared", "count": count}

            persisted = load_goal(sid)
            expected_persisted_json = expected_persisted_json or (
                state.to_json() if state is not None else None
            )
            if expected_persisted_json is not None and (
                persisted is None or persisted.to_json() != expected_persisted_json
            ):
                return _failure(
                    "goal_persistence_failed",
                    "Goal action was not confirmed by persistent readback",
                    persisted=False,
                )
            return _success(normalized_action, state=state, change=change)
    except Exception as exc:
        return _failure(
            "goal_action_failed",
            f"failed to perform goal action {normalized_action!r}: {type(exc).__name__}: {exc}",
            persisted=False,
        )


set_goal = set_goal_tool


def check_goal_requirements() -> bool:
    return True


SET_GOAL_SCHEMA = {
    "name": "set_goal",
    "description": (
        "Manage the current interactive session's persistent standing goal with one tool. "
        "Use status/show/list actions for inspection. Use mutations only when the current user "
        "turn explicitly authorizes that exact operation. For set or draft, write one concise outcome, "
        "not a persona or implementation diary. Put objective proof in verification, non-negotiable "
        "limits in constraints and boundaries, and the exact stopping condition in stop_when. Remove "
        "generic exhortations, duplicate requirements, speculative edge cases, and repository rules "
        "already supplied elsewhere. After setting a goal, continue the first concrete step in the "
        "same turn. Resume never resets the model's spent turn budget."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(GOAL_ACTIONS),
                "description": "Goal operation to perform.",
            },
            "goal": {
                "type": "string",
                "description": "Concise persistent outcome for set or draft.",
            },
            "authorization_text": {
                "type": "string",
                "description": "Exact current-user-turn span explicitly authorizing this mutation.",
            },
            "max_turns": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional bounded turn budget for set/draft, capped by configuration.",
            },
            "contract": {
                "type": "object",
                "description": "Structured completion contract for set/draft.",
                "properties": {
                    "verification": {"type": "string"},
                    "constraints": {"type": "string"},
                    "boundaries": {"type": "string"},
                    "stop_when": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "replace_existing": {
                "type": "boolean",
                "default": False,
                "description": "Replace an active/paused goal only with explicit replacement authorization.",
            },
            "pid": {
                "type": "integer",
                "minimum": 1,
                "description": "Process ID for wait.",
            },
            "reason": {"type": "string", "description": "Optional pause/wait reason."},
            "text": {"type": "string", "description": "Subgoal text for subgoal_add."},
            "index": {
                "type": "integer",
                "minimum": 1,
                "description": "1-based subgoal or gate index for remove actions.",
            },
            "command": {"type": "string", "description": "Shell command for gate_add."},
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional gate timeout.",
            },
            "max_retries": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional gate retry count.",
            },
        },
        "required": ["action"],
    },
}


registry.register(
    name="set_goal",
    toolset="goal",
    schema=SET_GOAL_SCHEMA,
    handler=lambda args, **kw: set_goal_tool(
        action=args.get("action", "set"),
        goal=args.get("goal", ""),
        authorization_text=args.get("authorization_text"),
        max_turns=args.get("max_turns"),
        contract=args.get("contract"),
        replace_existing=bool(args.get("replace_existing", False)),
        pid=args.get("pid"),
        reason=args.get("reason", ""),
        text=args.get("text", ""),
        index=args.get("index"),
        command=args.get("command", ""),
        timeout_seconds=args.get("timeout_seconds"),
        max_retries=args.get("max_retries"),
        session_id=kw.get("session_id", ""),
        user_task=kw.get("user_task"),
        turn_id=kw.get("turn_id"),
        goal_control_revision=kw.get("goal_control_revision"),
        default_max_turns=kw.get("default_max_turns"),
    ),
    check_fn=check_goal_requirements,
    emoji="⊙",
)
