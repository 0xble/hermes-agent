#!/usr/bin/env python3
"""Model-callable activation of the existing per-session goal loop."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Optional

from tools.registry import registry, tool_error, tool_result

_ACTIVATION_RE = re.compile(
    r"(?:\b(?:set|create|start|activate|establish|make|replace|overwrite|supersede|switch|change)\b.{0,80}\b(?:standing\s+|active\s+|current\s+)?goal\b"
    r"|\b(?:standing\s+|active\s+|current\s+)?goal\b.{0,80}\b(?:set|create|start|activate|establish|make|replace|overwrite|supersede|switch|change)\b)",
    re.IGNORECASE | re.DOTALL,
)
_NEGATED_ACTIVATION_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never|without)\b.{0,48}"
    r"\b(?:set|create|start|activate|establish|make|replace|overwrite|"
    r"supersede|switch|change)\b",
    re.IGNORECASE | re.DOTALL,
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
    """Return the sentence containing a model-selected authorization span."""
    sentence_start = max(
        user_task.rfind(separator, 0, span_start) for separator in ".!?\n"
    ) + 1
    after_span = span_start + span_length
    sentence_ends = [
        position
        for separator in ".!?\n"
        if (position := user_task.find(separator, after_span)) >= 0
    ]
    sentence_end = min(sentence_ends) + 1 if sentence_ends else len(user_task)
    return user_task[sentence_start:sentence_end]


def _normalize_max_turns(max_turns: Optional[int]) -> Optional[int]:
    if max_turns is None:
        return None
    if isinstance(max_turns, bool):
        raise ValueError("max_turns must be a positive integer")
    if isinstance(max_turns, float) and not max_turns.is_integer():
        raise ValueError("max_turns must be a positive integer")
    try:
        value = int(max_turns)
    except (TypeError, ValueError):
        raise ValueError("max_turns must be a positive integer") from None
    if value <= 0:
        raise ValueError("max_turns must be a positive integer")
    return value


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


def set_goal_tool(
    goal: str,
    *,
    session_id: str,
    user_task: Optional[str] = None,
    authorization_text: Optional[str] = None,
    max_turns: Optional[int] = None,
    default_max_turns: Optional[int] = None,
    contract: Optional[Mapping[str, Any]] = None,
    replace_existing: bool = False,
    turn_id: Optional[str] = None,
    goal_control_revision: Optional[int] = None,
) -> str:
    """Authoritatively activate a goal for the current interactive session."""
    sid = session_id.strip() if isinstance(session_id, str) else ""
    if not sid:
        return _failure(
            "missing_session_scope",
            "set_goal requires trusted active session scope",
        )
    turn = str(turn_id or "").strip()
    if not turn:
        return _failure(
            "missing_turn_scope",
            "set_goal requires trusted active turn scope",
        )
    if goal_control_revision is None:
        return _failure(
            "missing_goal_control_revision",
            "set_goal requires goal-control authority captured at turn start",
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
    task_text = user_task if isinstance(user_task, str) else ""
    auth_text = authorization_text if isinstance(authorization_text, str) else ""
    if not task_text.strip() or not auth_text.strip():
        return _failure(
            "explicit_goal_authorization_required",
            "The current user turn must explicitly ask Hermes to set, create, start, or activate a goal",
        )
    auth_start = task_text.find(auth_text)
    if auth_start < 0:
        return _failure(
            "authorization_not_in_current_turn",
            "authorization_text must be an exact span from the current user turn",
        )
    auth_context = _authorization_sentence(task_text, auth_start, len(auth_text))
    if not _explicit_activation_requested(
        auth_text, task_text[:auth_start], auth_context
    ):
        return _failure(
            "explicit_goal_authorization_required",
            "The quoted authorization must be a direct instruction to activate a goal",
        )
    if not isinstance(goal, str):
        return _failure("invalid_goal", "goal must be a string")
    goal_text = goal.strip()
    if not goal_text:
        return _failure("invalid_goal", "goal text is empty")
    if contract is not None and not isinstance(contract, Mapping):
        return _failure("invalid_contract", "contract must be an object")

    try:
        default_turns = _resolve_default_max_turns(default_max_turns)
        turns = _normalize_max_turns(max_turns)
    except ValueError as exc:
        return _failure("invalid_max_turns", str(exc))
    if turns is not None and turns > default_turns:
        return _failure(
            "turn_budget_exceeded",
            f"max_turns ({turns}) exceeds configured goal budget ({default_turns})",
        )

    has_existing = False
    existing_goal = ""
    try:
        from hermes_cli.goals import (
            GoalContract,
            GoalManager,
            guard_goal_activation,
            load_goal,
        )

        with guard_goal_activation(sid, goal_control_revision) as authorized:
            if not authorized:
                return _failure(
                    "goal_activation_cancelled",
                    "A newer user goal-control command cancelled activation from this running turn",
                )
            manager = GoalManager(session_id=sid, default_max_turns=default_turns)
            existing = manager.state
            has_existing = bool(
                existing and existing.status in {"active", "paused"}
            )
            existing_goal = existing.goal if existing is not None else ""
            if has_existing and not replace_existing:
                return _failure(
                    "active_goal_exists",
                    "An active or paused goal already exists. Ask explicitly to replace it, then set replace_existing=true.",
                    existing_goal=existing_goal,
                )
            if has_existing and not _explicit_replacement_requested(
                auth_text, auth_context
            ):
                return _failure(
                    "explicit_replacement_authorization_required",
                    "Replacing an active or paused goal requires explicit replacement language in the quoted authorization",
                    existing_goal=existing_goal,
                )

            goal_contract = GoalContract.from_dict(dict(contract or {}))
            state = manager.set(goal_text, max_turns=turns, contract=goal_contract)
            persisted = load_goal(sid)
            persisted_ok = (
                persisted is not None and persisted.to_json() == state.to_json()
            )
            if not persisted_ok:
                return _failure(
                    "goal_persistence_failed",
                    "Goal activation was not confirmed by persistent readback",
                    persisted=False,
                )
    except Exception as exc:
        return _failure(
            "goal_activation_failed",
            f"failed to set goal: {type(exc).__name__}: {exc}",
            persisted=False,
        )

    return tool_result(
        success=True,
        persisted=True,
        status=state.status,
        goal=state.goal,
        max_turns=state.max_turns,
        replaced_existing=has_existing,
        replaced_goal=existing_goal if has_existing else None,
        authorization_text=auth_text,
        message="Goal set and active. Continue working toward it now.",
    )


# Stable Python API matching the model-facing tool name.
set_goal = set_goal_tool


def check_goal_requirements() -> bool:
    return True


SET_GOAL_SCHEMA = {
    "name": "set_goal",
    "description": (
        "Activate a persistent standing goal only when the current user turn explicitly asks "
        "Hermes to set, create, start, or activate one. Never infer goal activation from an "
        "ordinary task, question, recommendation, or draft request. After success, continue "
        "the first concrete step in this same turn. The existing goal hook will judge the "
        "response and continue automatically when incomplete."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "A concise persistent outcome Hermes should achieve.",
            },
            "authorization_text": {
                "type": "string",
                "description": (
                    "Exact quoted span from the current user turn that directly "
                    "authorizes activating or replacing a standing goal."
                ),
            },
            "max_turns": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional bounded turn budget, capped by the configured goal budget.",
            },
            "contract": {
                "type": "object",
                "description": "Optional structured completion contract using the existing goal fields.",
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
                "description": (
                    "Replace an active or paused goal. Use only when the current user turn "
                    "explicitly asks to replace the existing goal."
                ),
                "default": False,
            },
        },
        "required": ["goal", "authorization_text"],
    },
}


registry.register(
    name="set_goal",
    toolset="goal",
    schema=SET_GOAL_SCHEMA,
    handler=lambda args, **kw: set_goal_tool(
        goal=args.get("goal", ""),
        authorization_text=args.get("authorization_text"),
        max_turns=args.get("max_turns"),
        contract=args.get("contract"),
        replace_existing=bool(args.get("replace_existing", False)),
        session_id=kw.get("session_id", ""),
        user_task=kw.get("user_task"),
        turn_id=kw.get("turn_id"),
        goal_control_revision=kw.get("goal_control_revision"),
        default_max_turns=kw.get("default_max_turns"),
    ),
    check_fn=check_goal_requirements,
    emoji="⊙",
)
