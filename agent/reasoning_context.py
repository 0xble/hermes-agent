"""Task-local reasoning configuration for one complete conversation turn."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any


_TURN_REASONING: ContextVar[tuple[object, dict[str, Any]] | None] = ContextVar(
    "hermes_turn_reasoning",
    default=None,
)


class TurnReasoningContext:
    """Publish an explicit reasoning override without mutating shared agents."""

    def begin(
        self,
        owner: object,
        config: dict[str, Any] | None,
    ) -> Token[tuple[object, dict[str, Any]] | None] | None:
        if not isinstance(config, dict):
            return None
        return _TURN_REASONING.set((owner, dict(config)))

    def current(self, owner: object) -> dict[str, Any] | None:
        state = _TURN_REASONING.get()
        if state is None or state[0] is not owner:
            return None
        return dict(state[1])

    def end(
        self,
        token: Token[tuple[object, dict[str, Any]] | None] | None,
    ) -> None:
        if token is not None:
            _TURN_REASONING.reset(token)


turn_reasoning_context = TurnReasoningContext()
