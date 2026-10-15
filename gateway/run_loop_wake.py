"""Shared execution fences for gateway-authored loop wakes.

Non-loop events pass through without requiring runner state. Turn, inbound and
goal hosts share this owner so those paths also work on standalone mixins.
"""

from __future__ import annotations

import logging
from enum import Enum, auto
from typing import Any, Optional

logger = logging.getLogger(__name__)


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
        new_stamp = bool(metadata.get("loop_wakeup"))
        raw_tick = metadata.get("gateway_loop_tick")
        sid = metadata.get("gateway_session_id")
        if new_stamp:
            raw_tick = [metadata.get("loop_wakeup_instance"), metadata.get("loop_wakeup_tick")]
            sid = metadata.get("loop_wakeup_session_id") or sid
        if (not getattr(event, "internal", False)
                or (not new_stamp and not metadata.get("hermes_loop_wakeup"))
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
    ) -> Optional[tuple]:
        """Revalidate an admitted wake after an executor hop: ``(owning sid, stored control fence)``.

        Admission already started the tick. This stored-row read preserves cross-process controls;
        the synchronous event fence additionally covers local controls in the executor return gap.
        A route that moved to the fence session's compression successor follows the migrated row;
        any other route (a /resume elsewhere) defers.
        """
        from hermes_cli.loops import LoopManager
        from hermes_cli.session_controls import migrated_successor

        key_for_source = getattr(self, "_session_key_for_source", None)
        session_key = key_for_source(source) if callable(key_for_source) else None

        def _exact_started_tick() -> Optional[tuple]:
            lookup = getattr(getattr(self, "session_store", None), "lookup_by_session_key", None)
            current = lookup(session_key) if callable(lookup) and session_key else None
            owner = sid
            if callable(lookup) and session_key:
                if current is None:
                    return None
                if current.session_id != sid:
                    if migrated_successor("loop", sid) != current.session_id:
                        return None
                    owner = current.session_id
            state = LoopManager(owner).state
            if not (
                state is not None
                and state.status == "active"
                and state.awaiting_response
                and state.claim_phase == "started"
                and (state.created_at, state.ticks_fired) == tick
                and (run_generation is None or int(getattr(state, "run_generation", 0) or 0) == int(run_generation))
            ):
                return None
            return owner, int(getattr(state, "control_generation", 0) or 0)

        return await self._run_in_executor_with_context(_exact_started_tick)

    def _log_loop_wake_suppressed(self, event: Any, reason: str) -> None:
        """Name every fence-suppressed loop wake: the started tick stays for recovery to skip."""
        identity = self._loop_wake_command_identity(event)
        if identity is not None:
            logger.info("Discarding loop wake for session %s tick %s: %s", identity[0], identity[1], reason)

    def _loop_wake_execution_fence_sync(self, event: Any, fence: Optional[tuple]) -> bool:
        """Final local supersede check, with no yield before effect invocation.

        The stored admission fence remains the cross-process authority. This local check additionally
        prevents reset, stop, or loop controls in the executor-return gap and performs no SessionDB
        work on the gateway loop thread.
        """
        if fence is None:
            metadata = getattr(event, "metadata", None) or {}
            return (
                self._loop_wake_command_identity(event) is None
                or not metadata.get("gateway_session_key")
            )
        try:
            sid, tick, run_generation, control_generation = fence
            if control_generation is None:
                return False
        except (TypeError, ValueError):
            return False
        if self._loop_wake_command_identity(event) != (sid, tick):
            return False
        from hermes_cli.loops import loop_control_generation
        key_for_source = getattr(self, "_session_key_for_source", None)
        current_run = getattr(self, "_is_session_run_current", None)
        run_ok = (
            current_run(key_for_source(event.source), int(run_generation or 0))
            if callable(key_for_source) and callable(current_run) else True
        )
        return (
            loop_control_generation(sid, default=None) == int(control_generation)
            and run_ok
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
        metadata = getattr(event, "metadata", None) or {}
        stamped_wake = bool(metadata.get("loop_wakeup") or metadata.get("hermes_loop_wakeup"))
        if metadata.get("loop_wakeup") and not metadata.get("gateway_session_key") and not getattr(
            event, "_gateway_loop_execution_fence", None
        ):
            return True
        if stamped_wake and not callable(getattr(self, "_session_key_for_source", None)):
            identity = self._loop_wake_command_identity(event)
            if identity is not None:
                from hermes_cli.loops import loop_control_generation
                event._gateway_loop_execution_fence = (*identity, 0, loop_control_generation(identity[0]))
            return True
        identity = self._loop_wake_command_identity(event)
        if identity is None:
            return not stamped_wake
        sid, tick = identity
        fence = getattr(event, "_gateway_loop_execution_fence", None)
        generation = fence[2] if fence is not None else (
            self._current_session_run_generation(self._session_key_for_source(source))
            if hasattr(self, "_current_session_run_generation") else 0
        )
        current = await self._loop_wake_execution_current(
            sid, tick, source, run_generation=generation,
        )
        if current is None:
            return False
        owner, control = current
        if fence is not None and control != fence[3]:
            return False
        if owner != sid:
            if not metadata.get("loop_wakeup"):
                return False
            # Compression carried the started tick to the route's child; execution and settlement
            # follow it there, exactly as the model-boundary rebind would.
            from hermes_cli.session_controls import _rebind_wake_session
            _rebind_wake_session(metadata, "loop_wakeup_session_id", owner)
        if fence is None or owner != sid:
            fence = (owner, tick, generation, control)
            event._gateway_loop_execution_fence = fence
        return self._loop_wake_execution_fence_sync(event, fence)
