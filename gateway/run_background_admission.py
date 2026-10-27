"""Gateway-wide admission for turns nobody typed.

Restart resumes, goal and loop wakes, delegation retries and process or delegation completions
start new agent turns on their own. Unbounded, a restart or a busy hour starts a hundred of them
at once and every typed message then waits minutes on the shared state.db writer and event loop.
One ``BackgroundTurnGate`` (owned by the replay scheduler) admits them only while fewer than
``gateway.background_turn_concurrency`` gated turns run. Ungated origins do not consume these
slots, and typed input never asks the gate.

Restart resumes wait in the scheduler's priority queue. A wake is never made to wait: when the
gate is full it is refused with ``WakeNotAccepted`` and its caller keeps its durable claim (lifted
barrier, unclaimed loop tick, ledger row, requeued event) and retries on its own cadence. Waiting
in line would hold those claims past their leases, stall serial watchers, and deadlock a turn
whose post-turn hooks deliver another session's wake.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from gateway.replay_scheduler import BackgroundTurnGate, ReplayHandle, ReplayScheduler

logger = logging.getLogger("gateway.run")

DEFAULT_BACKGROUND_TURN_CONCURRENCY = 8
# Minimum gap between two background turn starts; a backlog drains as a trickle, not a burst.
BACKGROUND_TURN_SPACING_S = 1.0
# Origins (gateway.platforms.event.turn_origin_for_event) whose wakes ask the gate.
# Event attribute carrying a slot reserved before the caller took its durable claim.
BACKGROUND_RESERVATION_ATTR = "_background_turn_reservation"
_GATED_ORIGINS = frozenset({"goal_loop", "process"})


def gated_background_kind(event: Any) -> Optional[str]:
    """The gate lane for a wake nobody typed, or None when it is admitted without the gate."""
    from gateway.platforms.event import turn_origin_for_event
    origin = turn_origin_for_event(event)
    return origin if origin in _GATED_ORIGINS else None


class GatewayBackgroundAdmissionMixin:
    """Owns the shared replay scheduler and the background-turn gate in front of it."""

    def _build_replay_scheduler(self) -> ReplayScheduler:
        config = getattr(self, "config", None)
        gate = BackgroundTurnGate(
            getattr(config, "background_turn_concurrency", DEFAULT_BACKGROUND_TURN_CONCURRENCY),
            self._background_turn_keys, claimed_since=self._turn_started_since,
            closing=lambda: bool(getattr(self, "_draining", False)), spacing_s=BACKGROUND_TURN_SPACING_S,
        )
        return ReplayScheduler(getattr(config, "restart_replay_concurrency", 2), gate=gate)

    def _init_replay_scheduler(self) -> None:
        """Construct replay admission before any adapter can deliver an event."""
        self._replay_scheduler = self._build_replay_scheduler()

    def _get_replay_scheduler(self) -> ReplayScheduler:
        """Return the one gateway-wide replay admission scheduler.

        Bare test runners are often built with ``object.__new__``; retain a lazy
        fallback there while normal construction creates this before adapters start.
        """
        scheduler = self.__dict__.get("_replay_scheduler")
        if scheduler is None:
            scheduler = self.__dict__["_replay_scheduler"] = self._build_replay_scheduler()
        return scheduler

    def _enqueue_replay(
        self, *, priority: int, kind: str, session_key: Optional[str],
        dispatch: Callable[[], Any], profile_home: Any = None, on_drop: Optional[Callable[[], Any]] = None,
        gated: bool = False,
    ) -> ReplayHandle:
        """Queue one replay with its owning profile captured at enqueue time."""
        from gateway.run import _async_profile_runtime_scope
        captured_home = profile_home
        scheduler = self._get_replay_scheduler()

        async def _scoped_call(callback) -> Any:
            from contextlib import nullcontext
            from inspect import isawaitable
            scope = nullcontext()
            if captured_home is not None and getattr(self.config, "multiplex_profiles", False):
                scope = _async_profile_runtime_scope(captured_home)
            async with scope:
                result = callback()
                return await result if isawaitable(result) else result

        return scheduler.enqueue(
            priority=priority,
            kind=kind,
            session_key=session_key,
            profile_home=captured_home,
            dispatch=lambda: _scoped_call(dispatch),
            on_drop=(lambda: _scoped_call(on_drop)) if on_drop is not None else None,
            gated=gated,
        )

    def _background_turn_keys(self) -> set[str]:
        """Live gated turns, including startup resumes until their full turn finishes.

        Ungated relays/cron never acquire a slot, so counting them could permanently block
        every gated wake. Resumes have unknown origin and use their existing lifecycle owner.
        """
        from gateway.platforms.event import turn_origin_for_event
        resumes = getattr(self, "_startup_resume_inflight", set())
        return {
            key for key, state in list(self._session_states().items())
            if state.turn.agent is not None and state.turn.event is not None
            and (turn_origin_for_event(state.turn.event) in _GATED_ORIGINS
                 or (key in resumes and turn_origin_for_event(state.turn.event) != "user"))
        }

    def _turn_started_since(self, session_key: Any, since: float) -> bool:
        """True once a turn claimed ``session_key`` at or after wall time ``since``."""
        state = self._session_states().get(session_key) if isinstance(session_key, str) else None
        return state is not None and state.conversation.last_turn_started_at >= since

    def _session_states(self) -> dict:
        # Read-only view; never creates per-session state for the gate's sake.
        return self.__dict__.get("_sessions") or {}

    def _background_admission_key(self, adapter: Any, event: Any) -> Optional[str]:
        metadata = getattr(event, "metadata", None) or {}
        try:
            return str(metadata.get("gateway_session_key") or "") or adapter._event_session_key(event)
        except Exception:
            logger.debug("background admission: session key unavailable", exc_info=True)
            return None

    def _admit_background_turn(self, adapter: Any, event: Any) -> Optional[object]:
        """Reserve a background turn slot for ``event`` or raise ``WakeNotAccepted``.

        Returns the reservation token (release it if the adapter then refuses the event), or
        None when no slot is needed: typed input, an ungated kind, or a session that already has a
        live turn, whose queue this wake joins without adding a turn. A token the caller reserved
        before taking a durable claim (``_reserve_background_turn``) is honored unconditionally,
        so a claimed /loop tick or cleared barrier is never refused for capacity afterwards.
        """
        from gateway.wake import WakeNotAccepted
        stamped = getattr(event, BACKGROUND_RESERVATION_ATTR, None)
        if stamped is not None:
            return stamped
        kind = gated_background_kind(event)
        if kind is None:
            return None
        key = self._background_admission_key(adapter, event)
        if key and (self._is_session_running(key) or key in getattr(adapter, "_active_sessions", {})):
            return None
        token = self._try_reserve_background_turn(key or ("event", id(event)))
        if token is None:
            reason = ("startup restore in progress" if getattr(self, "_startup_restore_in_progress", False)
                      else "background turn limit reached")
            logger.info("Background turn deferred: kind=%s session_key=%s (%s)", kind, key or "", reason)
            raise WakeNotAccepted(f"{reason}; retry later")
        return token

    def _try_reserve_background_turn(self, key: Any) -> Optional[object]:
        """A reservation token, or None while the gate is full or the boot restore runs.

        A booting gateway admits no wake until its startup restore has finished: restored typed
        input and the queued restart resumes go first, and a liveness-watchdog restart does not
        come back up into the same stampede that took it down.
        """
        if getattr(self, "_startup_restore_in_progress", False):
            return None
        return self._get_replay_scheduler().gate.try_reserve(key)

    def _release_background_turn(self, token: Optional[object]) -> None:
        if token is not None:
            self._get_replay_scheduler().gate.release(token)

    def _reserve_background_turn(self, session_key: str, event: Any = None) -> Optional[object]:
        """Reserve a slot before taking a durable claim; None when the limit is reached.

        Stamp the returned token on the wake (``event``, or later via ``BACKGROUND_RESERVATION_ATTR``)
        so admission honors it, and release it on every path that does not admit.
        """
        token = self._try_reserve_background_turn(session_key)
        if token is not None and event is not None:
            setattr(event, BACKGROUND_RESERVATION_ATTR, token)
        return token

    def _background_turn_room(self) -> bool:
        """Whether a background turn could start now (a cheap pre-check before scan work)."""
        gate = self._get_replay_scheduler().gate
        return not getattr(self, "_startup_restore_in_progress", False) and gate.load() < gate.limit

    def _queued_resume_preclaims(self) -> int:
        """Restart resumes still waiting for a slot; they hold a preclaim but run no turn yet."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        inflight = getattr(self, "_startup_resume_inflight", None) or ()
        states = self._session_states()
        return sum(
            1 for key in inflight
            if (state := states.get(key)) is not None
            and state.turn.agent is _AGENT_PENDING_SENTINEL and state.turn.event is None
        )

    def _promote_background_admission(self, session_key: str) -> None:
        """A person is now waiting on this session: start its queued background turn now."""
        scheduler = self.__dict__.get("_replay_scheduler")
        if scheduler is not None and session_key:
            scheduler.promote(session_key)
