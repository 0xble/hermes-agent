"""Shared execution fences for gateway-authored loop wakes.

Non-loop events pass through without requiring runner state. Turn, inbound and
goal hosts share this owner so those paths also work on standalone mixins.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Any, Optional


class _CommandDispatch(Enum):
    """Truthy terminal dispatch without an effect: stop fallthrough, never settle a tick."""

    SUPPRESSED = auto()


_CommandHandled = bool | _CommandDispatch


class GatewayLoopWakeMixin:
    """Revalidate loop admission before preparation or effect invocation."""

    @staticmethod
    def _loop_wake_command_identity(event: Any) -> Optional[tuple]:
        """``(session_id, tick)`` for a gateway-authored /loop wake carrying an exact tick, else None."""
        metadata = getattr(event, "metadata", None) or {}
        raw_tick = metadata.get("gateway_loop_tick")
        sid = metadata.get("gateway_session_id")
        if (not getattr(event, "internal", False) or not metadata.get("hermes_loop_wakeup")
                or not sid or not isinstance(raw_tick, (list, tuple)) or len(raw_tick) != 2):
            return None
        return sid, tuple(raw_tick)

    async def _loop_wake_current_entry(self, sid: str, source: Any) -> Any:
        current = await self._run_in_executor_with_context(
            self.session_store.lookup_by_session_key, self._session_key_for_source(source),
        )
        return current if current is not None and current.session_id == sid else None

    async def _loop_wake_execution_current(
        self, sid: str, tick: tuple, source: Any, *, run_generation: Optional[int] = None,
    ) -> Optional[int]:
        """Revalidate an admitted wake after an executor hop and return the stored control fence.

        Admission already started the tick. This stored-row read preserves cross-process controls;
        the synchronous event fence additionally covers local controls in the executor return gap.
        """
        from hermes_cli.loops import LoopManager

        session_key = self._session_key_for_source(source)

        def _exact_started_tick() -> Optional[int]:
            current = self.session_store.lookup_by_session_key(session_key)
            if current is None or current.session_id != sid:
                return None
            state = LoopManager(sid).state
            if not (
                state is not None
                and state.status == "active"
                and state.awaiting_response
                and state.claim_phase == "started"
                and (state.created_at, state.ticks_fired) == tick
                and (run_generation is None or int(getattr(state, "run_generation", 0) or 0) == int(run_generation))
            ):
                return None
            return int(getattr(state, "control_generation", 0) or 0)

        return await self._run_in_executor_with_context(_exact_started_tick)

    def _loop_wake_execution_fence_sync(self, event: Any, fence: Optional[tuple]) -> bool:
        """Final local supersede check, with no yield before effect invocation.

        The stored admission fence remains the cross-process authority. This local check additionally
        prevents reset, stop, or loop controls in the executor-return gap and performs no SessionDB
        work on the gateway loop thread.
        """
        if fence is None:
            return self._loop_wake_command_identity(event) is None
        try:
            sid, tick, run_generation, control_generation = fence
            if control_generation is None:
                return False
        except (TypeError, ValueError):
            return False
        if self._loop_wake_command_identity(event) != (sid, tick):
            return False
        from hermes_cli.loops import loop_control_generation
        return (
            loop_control_generation(sid) == int(control_generation)
            and self._is_session_run_current(
                self._session_key_for_source(event.source), int(run_generation or 0),
            )
        )

    async def _invoke_loop_wake_effect(self, event: Any, handler, *args, **kwargs):
        """Revalidate the admitted token off-loop, then enter the effect without yielding."""
        fence = getattr(event, "_gateway_loop_execution_fence", None)
        if fence is not None:
            sid, tick, run_generation, control_generation = fence
            if control_generation is None:
                return False, None
            from hermes_cli.loops import LoopManager
            def _check_loop_execution():
                return LoopManager(sid)._tick_execution_current(
                    tick, control_generation=int(control_generation), run_generation=int(run_generation or 0),
                )
            if not await self._run_in_executor_with_context(_check_loop_execution):
                return False, None
        if not self._loop_wake_execution_fence_sync(event, fence):
            return False, None
        return True, await handler(*args, **kwargs)

    def _invoke_loop_wake_sync_effect(self, event: Any, handler, *args):
        """Revalidate and fence a synchronous plugin in its invocation worker."""
        fence = getattr(event, "_gateway_loop_execution_fence", None)
        if not self._loop_wake_execution_fence_sync(event, fence):
            return False, None
        if fence is not None:
            sid, tick, run_generation, control_generation = fence
            if control_generation is None:
                return False, None
            from hermes_cli.loops import LoopManager
            if not LoopManager(sid)._tick_execution_current(
                tick, control_generation=int(control_generation), run_generation=int(run_generation or 0),
            ):
                return False, None
        if not self._loop_wake_execution_fence_sync(event, fence):
            return False, None
        return True, handler(*args)

    async def _prepare_loop_wake_execution(self, event: Any, source: Any) -> bool:
        """Revalidate the one admission token before preparation, hooks or command dispatch."""
        identity = self._loop_wake_command_identity(event)
        if identity is None:
            return not (getattr(event, "metadata", None) or {}).get("hermes_loop_wakeup")
        sid, tick = identity
        fence = getattr(event, "_gateway_loop_execution_fence", None)
        generation = fence[2] if fence is not None else self._current_session_run_generation(
            self._session_key_for_source(source),
        )
        control = await self._loop_wake_execution_current(
            sid, tick, source, run_generation=generation,
        )
        if control is None or (fence is not None and control != fence[3]):
            return False
        if fence is None:
            fence = (sid, tick, generation, control)
            event._gateway_loop_execution_fence = fence
        return self._loop_wake_execution_fence_sync(event, fence)
