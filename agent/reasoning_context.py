"""Task-local ownership for one-turn reasoning overrides.

The active override lives in a ContextVar instead of on ``AIAgent``. This keeps
concurrent turns on a cached agent isolated while allowing child work spawned by
the same turn to inherit its reasoning policy.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any


@dataclass
class _TurnReasoningState:
    owner: object
    config: dict[str, Any] | None = None


_CURRENT_TURN_REASONING: ContextVar[_TurnReasoningState | None] = ContextVar(
    "hermes_turn_reasoning", default=None
)


def begin_turn_reasoning(owner: object) -> Token[_TurnReasoningState | None]:
    """Begin an owned reasoning scope and return its restoration token."""
    return _CURRENT_TURN_REASONING.set(_TurnReasoningState(owner=owner))


def set_turn_reasoning_config(owner: object, config: dict[str, Any]) -> bool:
    """Set the active scope's override when ``owner`` owns that scope."""
    state = _CURRENT_TURN_REASONING.get()
    if state is None or state.owner is not owner:
        return False
    state.config = dict(config)
    return True


def get_turn_reasoning_config(owner: object) -> dict[str, Any] | None:
    """Return a copy of ``owner``'s current override, if one is active."""
    state = _CURRENT_TURN_REASONING.get()
    if state is None or state.owner is not owner or state.config is None:
        return None
    return dict(state.config)


def reset_turn_reasoning(token: Token[_TurnReasoningState | None]) -> None:
    """Restore the reasoning scope represented by ``token``."""
    _CURRENT_TURN_REASONING.reset(token)
