"""Task-local ownership for one-turn reasoning overrides.

The active override lives in a ContextVar instead of on ``AIAgent``. This keeps
concurrent turns on a cached agent isolated while allowing child work that
inherits the current context to see the same turn setting.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any


_SOURCE_PRIORITY = {
    "baseline": 0,
    "adaptive": 10,
    "hook": 50,
    "explicit": 100,
}


@dataclass
class _TurnReasoningState:
    owner: object
    config: dict[str, Any] | None = None
    source: str = "baseline"
    priority: int = 0


_CURRENT_TURN_REASONING: ContextVar[_TurnReasoningState | None] = ContextVar(
    "hermes_turn_reasoning", default=None
)


def _priority(source: str) -> int:
    return _SOURCE_PRIORITY.get(source, _SOURCE_PRIORITY["hook"])


def begin_turn_reasoning(
    owner: object,
    config: dict[str, Any] | None = None,
    *,
    source: str = "baseline",
) -> Token:
    """Begin a turn-owned scope and return the token required to restore it."""
    state = _TurnReasoningState(
        owner=owner,
        config=dict(config) if isinstance(config, dict) else None,
        source=source,
        priority=_priority(source) if isinstance(config, dict) else 0,
    )
    return _CURRENT_TURN_REASONING.set(state)


def set_turn_reasoning_config(
    owner: object,
    config: dict[str, Any],
    *,
    source: str = "hook",
) -> bool:
    """Set the active scope's override when owner and precedence permit it."""
    state = _CURRENT_TURN_REASONING.get()
    priority = _priority(source)
    if state is None or state.owner is not owner or priority < state.priority:
        return False
    state.config = dict(config)
    state.source = source
    state.priority = priority
    return True


def get_turn_reasoning_config(owner: object) -> dict[str, Any] | None:
    """Return a copy of ``owner``'s current override, if one is active."""
    state = _CURRENT_TURN_REASONING.get()
    if state is None or state.owner is not owner or state.config is None:
        return None
    return dict(state.config)


def get_turn_reasoning_source(owner: object) -> str | None:
    """Return the source label for ``owner``'s active override."""
    state = _CURRENT_TURN_REASONING.get()
    if state is None or state.owner is not owner or state.config is None:
        return None
    return state.source


def reset_turn_reasoning(token: Token) -> None:
    """Restore the reasoning scope that preceded ``token``."""
    _CURRENT_TURN_REASONING.reset(token)
