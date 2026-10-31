"""Goal/heartbeat continuation, post-turn hooks and loop-wakeup watcher methods for GatewayRunner.

Split out of ``gateway/run.py``; bound onto ``GatewayRunner`` via the MRO.
``gateway.run`` internals are imported lazily inside method bodies (import cycle),
so ``patch("gateway.run.X")`` keeps intercepting them at call time.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
import time
from contextlib import nullcontext, suppress
from typing import TYPE_CHECKING, Any, Dict, Optional

from gateway.config import Platform
from gateway.platforms.event import GOAL_CONTINUATION_METADATA_KEY, MessageEvent, MessageType
from hermes_cli.goal_notices import goal_notices_enabled
from gateway.run_background_admission import BACKGROUND_RESERVATION_ATTR
from gateway.run_loop_wake import GatewayLoopWakeMixin

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.run import GatewayRunner
    from gateway.run_turn_runner import TurnRunner

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")


# Parallels the CLI's "user-interrupted (Ctrl+C)": an explicit pause that user input never revives.
_GOAL_STOP_PAUSE_REASON = "user-interrupted (/stop)"

# The retry runs as a tracked background task so post-delivery callbacks and the next turn do not
# wait on Telegram's flood window. The shared short-wait helper owns the 15s budget and 0.5s slack;
# this attempt cap bounds additional traffic when Telegram keeps extending the penalty.
_GOAL_NOTICE_MAX_ATTEMPTS = 3


def _goal_notice_kind(message: str) -> str:
    """Stable warning label for the user-visible goal state a notice represents."""
    text = str(message or "")
    lowered = text.lower()
    if text.startswith("⚠ Goal judge unavailable"):
        return "failed"
    if text.startswith("⏳"):
        return "parked"
    if text.startswith("↻"):
        return "continuing"
    if text.startswith("✓"):
        return "achieved"
    if text.startswith("✗"):
        return "failed"
    if text.startswith("▶"):
        return "resumed"
    if text.startswith("⏸"):
        return "blocked" if "blocked" in lowered else "paused"
    if "blocked" in lowered:
        return "blocked"
    return "status"


def _goal_notice_outbound_class(message: str, notice_kind: Optional[str] = None) -> str:
    """Protect outcomes/control receipts, not routine automatic status chatter.

    Infer the actual outcome before a caller hint: an idle lifetime-ceiling pause
    arrives on the same wait-age path as a routine recheck. Retries use this same
    classification after the first send's context exits.
    """
    from gateway.platforms.base import OUTBOUND_FINAL, OUTBOUND_NOTICE

    kind = _goal_notice_kind(message)
    if kind == "status":
        kind = notice_kind or kind
    return OUTBOUND_FINAL if kind in {"achieved", "blocked", "paused", "failed", "resumed"} else OUTBOUND_NOTICE


class GatewayGoalsMixin(GatewayLoopWakeMixin):
    """Goal/heartbeat continuation, post-turn hooks and loop-wakeup watcher methods for GatewayRunner."""

    # ── /goal — persistent cross-turn goals (Ralph-style loop) ──────────
    def _goal_max_turns_from_config(self) -> int:
        """Configured /goal turn budget. GatewayRunner.config is a GatewayConfig dataclass, so the
        top-level ``goals`` block is only reachable via hermes_cli.config.load_config()."""
        try:
            goals_cfg = (
                (self.config or {}).get("goals", {})
                if isinstance(self.config, dict)
                else getattr(self.config, "goals", {}) or {}
            )
            if not goals_cfg:
                from hermes_cli.config import load_config

                goals_cfg = (load_config() or {}).get("goals") or {}
            from hermes_cli.goals import normalize_goal_max_turns

            return normalize_goal_max_turns(goals_cfg.get("max_turns", 20))
        except Exception:
            return 20

    def _goal_min_continuation_gap_from_config(self) -> float:
        """Configured minimum interval between autonomous goal continuations."""
        try:
            goals_cfg = (
                (self.config or {}).get("goals", {})
                if isinstance(self.config, dict)
                else getattr(self.config, "goals", {}) or {}
            )
            if not goals_cfg:
                from hermes_cli.config import load_config
                goals_cfg = (load_config() or {}).get("goals") or {}
            from hermes_cli.goals import normalize_goal_continuation_gap
            return normalize_goal_continuation_gap(goals_cfg.get("min_continuation_gap_seconds", 15 * 60))
        except Exception:
            return 15 * 60

    async def _warm_goals_session_db(self, label: str) -> None:
        """Warm the goals SessionDB cache off-loop (best-effort): a cold cache runs the state.db
        init on the loop thread and freezes the loop. The executor hop keeps the profile home
        override alive under multiplex; a failed warm-up is a bounded stall, never a crash."""
        try:
            from hermes_cli.goals import _get_session_db as _warm_goals_db

            await self._run_in_executor_with_context(_warm_goals_db)
        except Exception as exc:
            logger.warning("%s: session DB warm-up failed: %s", label, exc)

    async def _session_entry_for_manager(self, event: MessageEvent, label: str):
        """Session entry for a /goal or /heartbeat manager, or None when lookup fails. Warms the
        SessionDB cache first (a cold cache drops the first write while the reply claims it was
        set). Internal events never touch activity (idle/daily reset clock)."""
        await self._warm_goals_session_db(label)
        try:
            session_entry = await self.async_session_store.get_or_create_session(
                event.source, touch_activity=not bool(getattr(event, "internal", False)),
            )
        except Exception as exc:
            logger.debug("%s: session lookup failed: %s", label, exc)
            return None
        return session_entry if getattr(session_entry, "session_id", None) else None

    async def _manager_for_event(self, event: MessageEvent, kind: str, load):
        """``(manager, session_entry)`` for *kind* ("goal"/"heartbeat"), or ``(None, None)``.
        ``load()`` imports the manager class and returns a ``session_id -> manager`` factory."""
        try:
            factory = load()
        except Exception as exc:
            logger.debug("%s manager unavailable: %s", kind, exc)
            return None, None
        session_entry = await self._session_entry_for_manager(event, f"{kind} manager")
        if session_entry is None:
            return None, None
        return await self._run_in_executor_with_context(factory, session_entry.session_id), session_entry

    async def _get_goal_manager_for_event(self, event: MessageEvent):
        """Return ``(GoalManager, session_entry)`` for this event, or ``(None, None)``."""
        def _load():
            from hermes_cli.goals import GoalManager
            max_turns = self._goal_max_turns_from_config()
            min_gap = self._goal_min_continuation_gap_from_config()
            return lambda sid: GoalManager(
                session_id=sid, default_max_turns=max_turns, min_continuation_gap_seconds=min_gap,
            )
        return await self._manager_for_event(event, "goal", _load)

    async def _get_heartbeat_manager_for_event(self, event: MessageEvent):
        """Return ``(HeartbeatManager, session_entry)`` for this event, or ``(None, None)``."""
        def _load():
            from hermes_cli.heartbeat import HeartbeatManager
            return lambda sid: HeartbeatManager(session_id=sid)
        return await self._manager_for_event(event, "heartbeat", _load)

    @staticmethod
    def _synthetic_prompt_event(
        source: Any, text: str, *, internal: bool = False, reply_expected: Optional[bool] = None,
        goal_continuation: bool = False, goal_session_id: str = "",
        goal_state: Any = None, goal_fingerprint: Optional[str] = None, goal_instance: Optional[float] = None,
    ) -> MessageEvent:
        """Build the TEXT event used to inject a goal/heartbeat/loop prompt into a session.

        The stored source's ``message_id`` is the message that registered the watch; a synthetic
        prompt is not a reply to it, so it is dropped or every progress bubble and final reply
        would quote that stale message (Telegram DM topics route anchorless via the topic id).

        ``reply_expected=False`` marks a gateway-authored wake that may end silently (a goal
        continuation on a no-change tick). A typed message absorbed into the same turn still
        restores the human contract through ``MessageEvent.absorb_reply_expected``.
        ``replace_source`` keeps the receiving-adapter provenance native admission reads.
        """
        metadata = {}
        if goal_continuation:
            from hermes_cli.session_controls import _definition_fingerprint
            # Instance fences replacement; definition edits refresh when the queue is consumed.
            fingerprint = goal_fingerprint if goal_fingerprint is not None else _definition_fingerprint(
                "goal", goal_state.to_json() if goal_state is not None else None,
            )
            metadata = {
                GOAL_CONTINUATION_METADATA_KEY: True,
                "goal_continuation_session_id": goal_session_id,
                "goal_continuation_fingerprint": fingerprint,
                "goal_continuation_instance": (
                    goal_instance if goal_instance is not None else getattr(goal_state, "created_at", None)
                ),
                "goal_continuation_created_at": time.time(),
            }
        if getattr(source, "message_id", None):
            from gateway.session_identity import replace_source
            source = replace_source(source, message_id=None)
        event = MessageEvent(
            text=text, message_type=MessageType.TEXT, source=source, internal=internal,
            reply_expected=reply_expected,
            metadata=metadata,
            inherit_channel_pin=True,
        )
        from gateway.session_synthetic import mark_synthetic
        mark_synthetic(event)
        return event

    def _register_heartbeat_watch(self, quick_key: str, source: Any, session_id: str) -> None:
        """Track the canonical route and start the restart-recoverable poller."""
        watch = getattr(self, "_heartbeat_watch", None)
        if watch is None:
            watch = self._heartbeat_watch = {}
        watch[quick_key] = (source, session_id)
        self._start_heartbeat_poller()

    def _unregister_heartbeat_watch(self, quick_key: str) -> None:
        (getattr(self, "_heartbeat_watch", None) or {}).pop(quick_key, None)

    async def _heartbeat_poll_once(self, watch: dict) -> None:
        """Wake each idle watched session once; leave busy sessions' ticks unclaimed."""
        for quick_key, (source, session_id) in list(watch.items()):
            try:
                with self._profile_scope_for_source(source):
                    await self._heartbeat_poll_watch(watch, quick_key, source, session_id)
            except Exception as exc:
                logger.debug("heartbeat poll for %s failed: %s", quick_key, exc)

    async def _heartbeat_poll_watch(self, watch, quick_key, source, session_id):
        await self._warm_goals_session_db("heartbeat poll")
        store = getattr(self, "session_store", None)
        if store is not None:
            current = store.peek_session_id(quick_key)
            if not current:
                watch.pop(quick_key, None)
                return
            session_id = current
            watch[quick_key] = (source, session_id)
        adapter = self._delivery_adapter_for(source)
        if adapter is None or not adapter._message_handler:
            return
        if getattr(self, 'session_authority', None) is not None:
            from gateway.run_heartbeat_acceptance import admit_heartbeat
            await admit_heartbeat(self, adapter, source, session_id, quick_key)
            return
        if (
            self._is_session_running(quick_key)
            or quick_key in adapter._active_sessions
            or self._queue_depth(quick_key, adapter=adapter) > 0
        ):
            return  # keep missed intervals due until user work has drained
        from agent.estop import check_paused

        if check_paused("heartbeat", logger):
            return  # `hermes pause`: leave the tick unclaimed so it fires after `hermes resume`
        from hermes_cli.heartbeat import HeartbeatManager

        mgr = HeartbeatManager(session_id=session_id)
        if not mgr.has_heartbeat():
            watch.pop(quick_key, None)
            return
        prompt = mgr.due_prompt()
        if not prompt:
            return
        event = self._synthetic_prompt_event(source, prompt)
        event.metadata["gateway_session_key"] = quick_key
        event._heartbeat_execution_started = False
        # Provenance read by display_kind_for_event / the turn's quiet surfaces; the event stays
        # non-internal so authorization and the emergency stop still apply.
        event._heartbeat_session_id = session_id
        # A pinned route skips topic recovery: no await between the idle
        # check and adapter claim. FIFO alone never wakes an idle session.
        try:
            await adapter.handle_message(event)
        finally:
            task = getattr(adapter, "_session_tasks", {}).get(quick_key)
            if task is not None:
                from gateway.run_heartbeat_acceptance import settle_heartbeat_attempt
                task.add_done_callback(lambda done: settle_heartbeat_attempt(event, mgr))
            elif quick_key not in adapter._active_sessions:
                mgr.abandon_fire()

    def _start_heartbeat_poller(self) -> None:
        """Start the single gateway-wide heartbeat poll task (idempotent)."""
        existing = getattr(self, "_heartbeat_poll_task", None)
        if existing is not None and not existing.done():
            return

        from hermes_cli.heartbeat import POLL_SECONDS

        async def _poll_loop():
            while True:
                await asyncio.sleep(POLL_SECONDS)
                from gateway.run_heartbeat_restore import restore_heartbeat_watches
                await restore_heartbeat_watches(self)
                watch = getattr(self, "_heartbeat_watch", None)
                if watch:
                    await self._heartbeat_poll_once(watch)
                from gateway.run_checkins import poll_checkins
                try:
                    await poll_checkins(self)
                except Exception:
                    logger.warning("Supervisor scan deferred", exc_info=True)

        try:
            task = self._heartbeat_poll_task = asyncio.create_task(_poll_loop())
            # PERMANENT once started (infinite loop) — tag it like a _spawn_supervised watcher so
            # _scale_to_zero_has_live_background_work() doesn't treat the gateway as busy forever.
            task._hermes_supervised_watcher = True  # type: ignore[attr-defined]
            _bg = getattr(self, "_background_tasks", None)
            if _bg is not None:
                _bg.add(task)
                task.add_done_callback(_bg.discard)
        except Exception:
            logger.debug("Failed to start heartbeat poller", exc_info=True)

    def _goal_notice_adapter(self, source: Any):
        adapter = self._delivery_adapter_for(source)
        if not adapter:
            logger.debug("goal continuation: no adapter for %s", getattr(source, "platform", None))
        return adapter

    def _log_goal_status_notice_failure(
        self, message: str, error: Any, attempts: int, *, notice_kind: Optional[str] = None,
    ) -> None:
        logger.warning(
            "goal continuation: status send failed notice_kind=%s attempts=%d: %s",
            notice_kind or _goal_notice_kind(message), attempts, error or "unknown error",
        )

    def _track_goal_notice_retry(self, task: asyncio.Task) -> None:
        retain = getattr(self, "_retain_background_task", None)
        if callable(retain):
            retain(task)
            return
        tasks = getattr(self, "_background_tasks", None)
        if not isinstance(tasks, set):
            tasks = self._background_tasks = set()
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _retry_goal_status_notice(
        self, source: Any, message: str, metadata: Any, wait: float, already_waited: float,
        attempts: int, notice_kind: Optional[str] = None, *, goal_notice: bool = True,
    ) -> None:
        from gateway.delivery_ledger import short_flood_wait

        error: Any = "retry attempts exhausted"
        while attempts < _GOAL_NOTICE_MAX_ATTEMPTS:
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                logger.warning(
                    "goal continuation: status retry cancelled notice_kind=%s attempts=%d",
                    notice_kind or _goal_notice_kind(message), attempts,
                )
                raise
            if goal_notice and not goal_notices_enabled():
                return
            adapter = self._goal_notice_adapter(source)
            if not adapter:
                self._log_goal_status_notice_failure(
                    message, "delivery adapter unavailable", attempts, notice_kind=notice_kind,
                )
                return
            try:
                from gateway.platforms.base import outbound_class
                # The retry task runs after the first send's class context exited.
                with outbound_class(_goal_notice_outbound_class(message, notice_kind)):
                    result = await adapter.send(source.chat_id, message, metadata=metadata)
            except Exception as exc:
                error = exc
                result = None
            else:
                if result is None or getattr(result, "success", True):
                    return
                error = getattr(result, "error", "unknown error")
            attempts += 1
            next_wait = short_flood_wait(error, already_waited)
            if next_wait is None:
                self._log_goal_status_notice_failure(
                    message, error, attempts, notice_kind=notice_kind,
                )
                return
            already_waited += next_wait
            wait = next_wait

        self._log_goal_status_notice_failure(message, error, attempts, notice_kind=notice_kind)

    async def _send_goal_status_notice(
        self, source: Any, message: str, *, notice_kind: Optional[str] = None, goal_notice: bool = True,
    ) -> None:
        """Send a /goal judge status line without holding the turn open for flood recovery."""
        if goal_notice and not goal_notices_enabled():
            return
        if getattr(source, "platform", None) == Platform.LOCAL:
            # The local adapter's send is a no-op (viewers read the event stream): main's TUI
            # painted the verdict from ``status.update {kind: goal}``.
            from gateway.session_authorities import active_authority
            authority = active_authority(self)
            live = authority.sessions.get(source.chat_id) if authority is not None else None
            if live is not None:
                live.event_stream.publish(
                    source.chat_id, {"kind": "goal", "text": message}, event_type="status.update",
                )
            return
        adapter = self._goal_notice_adapter(source)
        if not adapter:
            return
        metadata = None
        with suppress(Exception):
            metadata = self._thread_metadata_for_source(source)
        from agent.notice_style import notice_metadata
        metadata = notice_metadata(metadata, platform=source.platform, style=getattr(message, "notice_style", None))
        from gateway.platforms.base import outbound_class
        try:
            with outbound_class(_goal_notice_outbound_class(message, notice_kind)):
                result = await adapter.send(source.chat_id, message, metadata=metadata)
        except Exception as exc:
            error = exc
            result = None
        else:
            if result is None or getattr(result, "success", True):
                return
            error = getattr(result, "error", "unknown error")

        from gateway.delivery_ledger import short_flood_wait
        wait = short_flood_wait(error)
        if wait is None:
            self._log_goal_status_notice_failure(
                message, error, attempts=1, notice_kind=notice_kind,
            )
            return
        task = asyncio.create_task(
            self._retry_goal_status_notice(
                source, message, metadata, wait, wait, attempts=1, notice_kind=notice_kind,
                goal_notice=goal_notice,
            ),
            name=f"goal-status-retry:{notice_kind or _goal_notice_kind(message)}",
        )
        self._track_goal_notice_retry(task)

    async def _defer_goal_status_notice_after_delivery(
        self, source: Any, message: str, *, goal_notice: bool = True,
    ) -> None:
        """Send a /goal status line after the main response is delivered.

        The adapter sends the agent response after this caller returns, so for reading order use
        its one-shot post-delivery callback when available, else deliver directly (never drop).
        """
        if goal_notice and not goal_notices_enabled():
            return
        adapter = self._goal_notice_adapter(source)
        if not adapter:
            return

        async def _deliver() -> None:
            try:
                # Adapters invoke post-delivery callbacks after the turn's profile scope exits.
                with self._profile_scope_for_source(source):
                    await self._send_goal_status_notice(source, message, goal_notice=goal_notice)
            except Exception as exc:
                logger.warning("goal continuation: status send failed: %s", exc, exc_info=True)

        session_key = None
        with suppress(Exception):
            session_key = self._session_key_for_source(source)
        if session_key and hasattr(adapter, "register_post_delivery_callback"):
            try:
                active = getattr(adapter, "_active_sessions", {}).get(session_key)
                generation = getattr(active, "_hermes_run_generation", None) if active is not None else None
                adapter.register_post_delivery_callback(session_key, _deliver, generation=generation)
                return
            except Exception as exc:
                logger.debug("goal continuation: post-delivery callback registration failed: %s", exc)
        await _deliver()

    async def _post_turn_manager(self, session_entry: Any, label: str, module: str, load):
        """Shared head of the post-turn hooks: ``load()`` imports the manager module and returns a
        ``session_id -> manager`` factory; None when unavailable / no session id. Warms the
        SessionDB cache first, then constructs/loads the manager off-loop too."""
        try:
            factory = load()
        except Exception as exc:
            logger.debug("%s: %s module unavailable: %s", label, module, exc)
            return None
        sid = getattr(session_entry, "session_id", None) or ""
        if not sid:
            return None
        await self._warm_goals_session_db(label)
        return await self._run_in_executor_with_context(factory, sid)

    async def _pause_goal_for_stop(
        self, session_key: str, source: Any, *, run_generation: Optional[int] = None,
    ) -> bool:
        """``/stop`` pauses the standing goal and drops its queued continuations (CLI Ctrl+C parity).

        A judge BLOCKED pause is overwritten too: an explicit stop must not be revived by the next
        user message, only by ``/goal resume``. Best-effort; a failure never breaks the stop.
        Returns True when a goal was paused by this call.
        """
        try:
            session_id = await asyncio.to_thread(
                self._lookup_session_id_under_store_lock, self.session_store, session_key)
        except Exception as exc:
            logger.debug("goal stop: session lookup failed for %s: %s", session_key, exc)
            session_id = None
        def is_current():
            return run_generation is None or self._is_session_run_current(session_key, run_generation)

        if not is_current():
            return False
        adapter = self._delivery_adapter_for(source)
        with suppress(Exception):
            self._clear_goal_pending_continuations(session_key, adapter)
        if not session_id:
            return False
        from hermes_cli.goals import GoalManager

        def _pause() -> Optional[str]:
            mgr = GoalManager(
                session_id=str(session_id),
                default_max_turns=self._goal_max_turns_from_config(),
                min_continuation_gap_seconds=self._goal_min_continuation_gap_from_config(),
            )
            if not mgr.has_goal():
                return None
            if mgr.state.status == "paused" and mgr.state.paused_reason == _GOAL_STOP_PAUSE_REASON:
                return None
            if not is_current():
                return None
            mgr.pause(reason=_GOAL_STOP_PAUSE_REASON)
            return mgr.state.goal

        with self._profile_scope_for_source(source):
            await self._warm_goals_session_db("goal stop")
            if not is_current():
                return False
            try:
                goal = await self._run_in_executor_with_context(_pause)
            except Exception as exc:
                logger.warning("goal stop: pause failed for session %s: %s", session_id, exc)
                return False
            if not goal or not is_current():
                return False
            logger.info("goal stop: paused standing goal for session %s", session_id)
            try:
                await self._send_goal_status_notice(
                    source, f"⏸ Goal paused: {goal}\nUse /goal resume to continue, or /goal clear to end it.",
                    notice_kind="paused",
                )
            except Exception as exc:
                logger.debug("goal stop: notice failed: %s", exc)
            return True

    def _post_turn_goal_identity(self, event) -> dict[str, Any]:
        """Plain-data identity of the turn whose response the post-turn goal judge evaluates."""
        if event is None:
            return {"user_turn": False, "goal_instance": None, "loop_tick": None}
        metadata = getattr(event, "metadata", None) or {}
        instance = metadata.get("goal_continuation_instance") if metadata.get(
            GOAL_CONTINUATION_METADATA_KEY) else None
        from hermes_cli.session_controls import LOOP_WAKE_METADATA_KEY
        loop_tick = None
        if metadata.get(LOOP_WAKE_METADATA_KEY):
            loop_tick = [metadata.get("loop_wakeup_instance"), metadata.get("loop_wakeup_tick")]
        else:
            # Older in-process callers stamped the same exact identity under this name. Keep the
            # compatibility path strict to a two-part tick; no text-only completion is accepted.
            legacy_tick = metadata.get("gateway_loop_tick")
            if isinstance(legacy_tick, (list, tuple)) and len(legacy_tick) == 2:
                loop_tick = list(legacy_tick)
        return {"user_turn": self._is_user_turn_event(event),
                "goal_instance": instance if type(instance) in (int, float) else None,
                "loop_tick": loop_tick}

    def _is_user_turn_event(self, event) -> bool:
        """An admitted turn the user sent, not a wake, continuation, heartbeat or relayed message."""
        return not (getattr(event, "internal", False)
                    or not getattr(event, "allow_gateway_control", True)
                    or getattr(event, "_heartbeat_session_id", None)
                    or self._is_goal_continuation_event(event))

    async def _revive_blocked_goal_for_user_turn(self, session_entry, source, event) -> None:
        """Resume only after turn admission, independently of model success or delivery."""
        if not self._is_user_turn_event(event):
            return
        from hermes_cli.goals import GoalManager

        with self._profile_scope_for_source(source):
            await self._warm_goals_session_db("goal recovery")
            # A warm handle still takes the writer lock for metadata reads/writes.
            max_turns = self._goal_max_turns_from_config()
            min_gap = self._goal_min_continuation_gap_from_config()

            def _revive():
                mgr = GoalManager(session_entry.session_id, default_max_turns=max_turns,
                                  min_continuation_gap_seconds=min_gap)
                return mgr if mgr.resume_for_user_input() else None

            mgr = await self._run_in_executor_with_context(_revive)
            if mgr is None:
                return
            try:
                from agent.notice_style import ContextNotice
                await self._send_goal_status_notice(
                    source, ContextNotice(f"▶ Goal resumed: {mgr.state.goal}"), notice_kind="resumed",
                )
            except Exception as exc:
                logger.debug("goal recovery notice failed: %s", exc)

    async def _post_turn_goal_continuation(
        self, *, session_entry: Any, source: Any, final_response: str,
        user_initiated: bool = False, external_event: bool = False,
        goal_instance: Optional[float] = None,
    ) -> None:
        """Run the goal judge after a gateway turn (AFTER delivery) and, if still active, enqueue a
        continuation through the adapter FIFO so a simultaneous real user message takes priority.
        """
        from gateway.wake import automatic_route_available

        def _load():
            from hermes_cli.goals import GoalManager
            max_turns = self._goal_max_turns_from_config()
            min_gap = self._goal_min_continuation_gap_from_config()
            return lambda sid: GoalManager(
                session_id=sid, default_max_turns=max_turns, min_continuation_gap_seconds=min_gap,
            )

        mgr = await self._post_turn_manager(session_entry, "goal continuation", "goals", _load)
        if mgr is None:
            return
        if not mgr.is_active():
            return
        # A continuation turn answers the goal instance it was stamped for. If that goal was
        # replaced or cleared and re-set while the turn ran, its response is not evidence for the
        # new goal: judging it would spend the new goal's turns and verdicts on old work.
        if goal_instance is not None and getattr(mgr.state, "created_at", None) != goal_instance:
            logger.info("goal continuation: turn answered a superseded goal instance; not judged")
            return

        # Probe only eligible goals, but before judging can spend a retired topic's turns.
        if source is not None and not await automatic_route_available(self, source):
            return

        _bg_procs, _active_deleg = None, 0
        with suppress(Exception):
            from hermes_cli.goals import count_active_delegations, gather_background_processes as _gather_bg
            # Only THIS session's processes (gateway turns register under turn_ctx.session_id):
            # subagents' pollers must not park the parent's goal.
            _bg_procs = _gather_bg(owner_task_id=getattr(session_entry, "session_id", None) or None)
            _active_deleg = count_active_delegations(getattr(session_entry, "session_id", None))

        # judge_goal() is a synchronous aux-LLM HTTP call (10-40 s; would block Discord heartbeats).
        # _run_in_executor_with_context carries the profile secret scope / aux runtime contextvars
        # without which aux credential resolution fails under multiplexing.
        decision = await self._run_in_executor_with_context(
            lambda: mgr.evaluate_after_turn(
                final_response or "", user_initiated=user_initiated, external_event=external_event,
                background_processes=_bg_procs,
                active_delegations=_active_deleg,
            ),
        )
        msg = decision.get("message") or ""
        # Routine parked/continuing/wait-age text remains durable bookkeeping and log output, but
        # never consumes chat budget. Outcome/control notices still follow the visible path.
        if msg and source is not None:
            notice_kind = _goal_notice_kind(msg)
            if notice_kind in {"achieved", "blocked", "paused", "failed", "resumed"}:
                await self._defer_goal_status_notice_after_delivery(source, msg)
            else:
                logger.info("goal continuation: routine status notice suppressed kind=%s message=%s",
                            notice_kind, msg)
        prompt = decision.get("continuation_prompt") or ""
        if not decision.get("should_continue") or not prompt or source is None:
            return
        if getattr(source, "platform", None) == Platform.LOCAL:
            # A local session's adapter FIFO has no reader: its turns are authority admissions.
            # Commit the continuation there, as main's TUI chained it after the turn.
            from gateway.session_automation import admit_route_wake
            try:
                await admit_route_wake(self, self._delivery_adapter_for(source), source, prompt, "goal-continuation")
            except Exception:  # health: allow BLE001 -- best-effort like the FIFO enqueue below; traceback logged
                logger.debug("goal continuation: local admission failed", exc_info=True)
            return
        # Enqueue via the adapter's FIFO so a user message already in flight preempts naturally.
        try:
            adapter = self._delivery_adapter_for(source)
            _quick_key = self._session_key_for_source(source)
            if adapter and _quick_key:
                # A goal continuation is gateway-authored: a no-change tick may answer NO_REPLY.
                self._enqueue_fifo(
                    _quick_key, self._synthetic_prompt_event(
                        source, prompt, reply_expected=False, goal_continuation=True,
                        goal_session_id=mgr.session_id, goal_state=mgr.state,
                    ), adapter,
                )
        except Exception as exc:
            logger.debug("goal continuation: enqueue failed: %s", exc)

    async def _run_post_turn_hooks(
        self, *, agent_result: Any, source: Any, is_internal: bool, event: Any = None,
    ) -> None:
        """Run goal and loop bookkeeping after an agent turn returns."""
        from gateway.session_authorities import active_authority
        from gateway.session_viewer_automation import viewer_driven
        if viewer_driven(active_authority(self), getattr(source, "chat_id", None)):
            return  # the classic CLI viewer judges and continues through ``session.automation``
        final_text = self._final_text_for_post_turn_hooks(agent_result, event)
        from hermes_cli.goal_turn_outcome import is_successful_goal_turn
        successful = not self._turn_failed() and is_successful_goal_turn(
            agent_result, final_text, succeeded=getattr(event, "_agent_turn_succeeded", None),
        )
        if not successful:
            final_text = ""
        try:
            session_entry = await self.async_session_store.get_or_create_session(
                source, touch_activity=not is_internal,
            )
        except Exception as exc:
            logger.debug("post-turn session resolution failed: %s", exc)
            return
        # Failed/interrupted turns stay out of /goal. Successful empty turns still carry
        # tool evidence; an in-flight /loop tick always needs release/rescheduling.
        # The terminal turn's identity: a queued chain carries its last turn's, not the head's.
        identity = getattr(event, "_post_turn_goal_identity", None)
        if not isinstance(identity, dict):
            identity = (self._post_turn_goal_identity(event) if event is not None
                        else {"user_turn": not is_internal, "goal_instance": None, "loop_tick": None})
        # The loop tick this turn (or chain) actually ran: the latest stamped loop wake in a queued
        # chain, else the head event's own. A turn that ran no loop wake owns no tick.
        loop_tick = getattr(event, "_post_turn_loop_tick", None) or identity.get("loop_tick")
        if not loop_tick and event is not None:
            loop_tick = self._post_turn_goal_identity(event).get("loop_tick")
        hooks = [("loop completion", self._post_turn_loop_completion,
                  {"loop_tick": tuple(loop_tick) if isinstance(loop_tick, (list, tuple)) else None})]
        if successful:
            # Any delivery into the session is fresh evidence and pierces a goal wait: process or
            # delegation results, watcher/cron/kanban wakes, plugin injections. Only the goal's own
            # pacing wakes (continuations, /loop ticks, heartbeats) stay on the quiet ladder. The
            # markers are gateway-set metadata, never derived from message text. A completion drained
            # behind a continuation head keeps its origin on the head event.
            metadata = getattr(event, "metadata", None) or {}
            from hermes_cli.session_controls import LOOP_WAKE_METADATA_KEY
            own_pacing_wake = is_internal and event is not None and (
                self._is_goal_continuation_event(event)
                or bool(metadata.get(LOOP_WAKE_METADATA_KEY))
                or bool(getattr(event, "_heartbeat_session_id", None))
                # Restart recovery re-runs an interrupted turn; it is not new outside evidence.
                or getattr(event, "_restart_note_expected", None) is not None
                or str(getattr(event, "_outbox_admission_id", "") or "").startswith("resume:")
            )
            external_event = (
                (
                    metadata.get("notification_origin") in {
                        "process_registry_synthetic", "delegation_result", "cron_delivery", "relay_delivery",
                    }
                    # A continuation may inherit the head event's notification metadata when a
                    # follow-up chain is drained. It is still only the continuation anchor unless
                    # the chain explicitly reports a different terminal event identity (the
                    # completion-drained-behind-continuation case).
                    and (
                        not self._is_goal_continuation_event(event)
                        or (
                            isinstance(getattr(event, "_post_turn_goal_identity", None), dict)
                            and identity.get("goal_instance") is None
                        )
                    )
                )
                or (
                    is_internal
                    and event is not None
                    and not own_pacing_wake
                    and metadata.get("notification_category") in {"result", "diagnostic"}
                )
            )
            if external_event and event is not None:
                # Trusted outside evidence pierces a long loop delay once. The durable mutation is
                # idempotent and refuses to move a tick that is already in flight, so duplicate
                # delivery notifications cannot create duplicate loop ticks.
                try:
                    from hermes_cli.loops import wake_loop_for_external_event
                    await self._run_in_executor_with_context(
                        wake_loop_for_external_event, str(session_entry.session_id)
                    )
                except Exception:
                    logger.debug("external event loop wake failed", exc_info=True)
            # A queued chain is judged once, on its terminal turn's response (identity above).
            goal_kwargs = {"user_initiated": bool(identity.get("user_turn")), "external_event": external_event}
            # A stamped continuation turn is judged only against the goal instance it served.
            instance = identity.get("goal_instance")
            if type(instance) in (int, float):
                goal_kwargs["goal_instance"] = instance
            hooks.insert(0, ("goal continuation", self._post_turn_goal_continuation, goal_kwargs))
        for label, hook, hook_kwargs in hooks:
            try:
                await hook(
                    session_entry=session_entry, source=source, final_response=final_text if successful else "",
                    **hook_kwargs,
                )
            except Exception as exc:
                logger.debug("%s hook failed: %s", label, exc)
        self._publish_local_control(source, only_if_present=True)

    def _publish_local_control(self, source: Any, *, only_if_present: bool = False) -> None:
        """A local (Desktop/Ink) session's goal or loop state just moved here: repaint its viewers' cards,
        whose loop countdown runs off the snapshot's ``next_due_at``."""
        if getattr(source, "platform", None) != Platform.LOCAL:
            return
        from gateway.session_authorities import active_authority
        from gateway.session_control_actions import publish
        publish(active_authority(self), source.chat_id, only_if_present=only_if_present)

    @staticmethod
    def _turn_failed() -> bool:
        """Whether the owner recorded this turn's result as failed (``session_ingress`` captures it
        around ``_handle_message``; turns outside an owner capture nothing)."""
        from gateway.session_results import execution_result
        captured = execution_result.get() or {}
        result = captured.get('terminal_result', captured.get('result'))
        return isinstance(result, dict) and bool(result.get('failed'))

    @staticmethod
    def _final_text_for_post_turn_hooks(agent_result, event=None) -> str:
        """Text for /goal and /loop after a gateway turn. Streamed turns return None from
        _handle_message_with_agent (already_sent); the delivered reply is stashed on the event."""
        # Explicit model output is authoritative even when empty. Delivery normalization can
        # manufacture notices that must never become goal/loop judgment evidence.
        model_text = getattr(event, "_agent_final_response", None)
        if isinstance(model_text, str):
            return model_text
        from hermes_cli.goal_turn_outcome import model_final_response
        if isinstance(agent_result, dict) and "final_response" in agent_result:
            return model_final_response(agent_result)
        # Older callers stash only the pre-marker-stripping delivery text.
        raw = getattr(event, "_raw_final_response", None)
        if isinstance(raw, str) and raw.strip():
            return raw
        text = ""
        if isinstance(agent_result, dict):
            text = str(agent_result.get("final_response") or "")
        elif isinstance(agent_result, str):
            text = agent_result
        if text.strip():
            return text
        streamed = getattr(event, "_streamed_final_response", None)
        return streamed if isinstance(streamed, str) and streamed.strip() else text

    async def _release_unsettled_loop_tick(self, event: Any, *, ran: bool) -> None:
        """Release a stamped loop claim when an admitted wake never reaches post-turn hooks.

        The scheduler has already marked the tick in flight before handing the event to the adapter.
        Admission can still return early, for example on the active-session cap or a lease timeout.
        Those paths must roll back exactly the wake they carried, and the DB write stays in the
        executor because SessionDB is not event-loop safe.
        """
        if ran or event is None or getattr(event, "_loop_tick_settled", False):
            return
        metadata = getattr(event, "metadata", None) or {}
        from hermes_cli.session_controls import LOOP_WAKE_METADATA_KEY
        if not metadata.get(LOOP_WAKE_METADATA_KEY) or metadata.get("loop_wakeup_command"):
            return
        session_id = metadata.get("loop_wakeup_session_id")
        instance = metadata.get("loop_wakeup_instance")
        fired = metadata.get("loop_wakeup_tick")
        if not isinstance(session_id, str) or not session_id or type(instance) not in (int, float) or type(fired) is not int:
            return
        from hermes_cli.loops import LoopManager
        manager = LoopManager(session_id=session_id)
        await self._run_in_executor_with_context(
            functools.partial(manager.abandon_tick, tick=(instance, fired))
        )
        event._loop_tick_settled = True

    async def _post_turn_loop_completion(
        self, *, session_entry: Any, source: Any, final_response: str,
        loop_tick: Optional[tuple] = None,
    ) -> None:
        """Complete a /loop wakeup tick after a gateway turn. No-op unless a tick is in flight
        (``awaiting_response``, set when the wakeup was injected); applies the LOOP_COMPLETE marker
        / --until judge / caps and schedules the next tick for the idle wakeup watcher.

        A normal user turn has no loop-tick identity. It must not settle a claimed wakeup merely
        because the session currently has one awaiting response; only the synthetic event carrying
        the exact ``gateway_loop_tick`` may do that.
        """
        def _load():
            from hermes_cli.loops import LoopManager
            return lambda sid: LoopManager(session_id=sid)

        mgr = await self._post_turn_manager(session_entry, "loop completion", "loops", _load)
        state = mgr.state if mgr is not None else None
        if state is None or not state.awaiting_response:
            return
        if loop_tick is None:
            if state.last_fired_at < getattr(self, "_gateway_started_at", 0.0):
                # A tick left by an older gateway has no reliable response identity. Skip it rather
                # than replaying an effect whose outcome is unknown; a tick claimed by this process
                # remains owned by its stamped wake.
                await self._run_in_executor_with_context(mgr.abandon_tick)
            return
        # The --until judge is a sync aux-LLM call — keep it off the event loop, but carry the
        # contextvars: a bare executor hop drops the profile HERMES_HOME override and secret scope,
        # so a served secondary's tick would be written into the DEFAULT profile's state.db.
        # A stamped wake completes only the tick it carried (a stop and re-set in between means a
        # newer tick is in flight that this response never answered).
        decision = await self._run_in_executor_with_context(
            functools.partial(mgr.complete_tick, final_response or "", tick=loop_tick))
        msg = decision.get("message") or ""
        if msg and source is not None:
            await self._defer_goal_status_notice_after_delivery(source, msg, goal_notice=False)

    async def _post_command_loop_completion(self, event: Any, source: Any) -> None:
        """Settle only a command dispatch that completed without falling through to model work."""
        identity = self._loop_wake_command_identity(event)
        if identity is None:
            return
        sid, tick = identity
        current = await self._loop_wake_current_entry(sid, source)
        if current is not None:
            await self._post_turn_loop_completion(
                session_entry=current, source=source, final_response="", loop_tick=tick,
            )

    async def _migrate_compressed_loop(self, sid: str, tip: str) -> None:
        """Retry a stranded parent's migration without orphan-clearing it on failure."""
        from hermes_cli.loops import migrate_loop_to_session

        migrated = await self._run_in_executor_with_context(
            migrate_loop_to_session, sid, tip, reason="compression",
        )
        if not migrated:
            logger.warning(
                "loop wakeup: compression migration failed for %s -> %s; leaving parent row",
                sid, tip,
            )

    async def _stale_resume_pending_marker(self, key: str, entry: Any) -> tuple:
        """Return ``(owned, stale_marker)`` for idle work that would wake *key*.

        Startup auto-resume owns a fresh restart marker. Once its bounded freshness window
        expires, it will no longer schedule this session; do not let that stale marker wedge
        otherwise eligible idle work forever. The snapshot lets the caller CAS-clear it so a
        successor that refreshed the marker wins rather than being stolen by the idle ticker.
        Legacy entries without last_resume_marked_at use updated_at, matching startup recovery.
        """
        if not getattr(entry, "resume_pending", False):
            return False, None
        from gateway.run import (
            _auto_continue_freshness_window, _is_fresh_gateway_interruption,
            _resume_pending_marker_timestamp,
        )

        marker = await self.async_session_store.get_resume_pending_marker(key)
        if marker is None:
            return True, None  # marker disappeared or the session was replaced; retry on the next scan
        marked_at = _resume_pending_marker_timestamp(entry, marker)
        if _is_fresh_gateway_interruption(marked_at, window_secs=_auto_continue_freshness_window()):
            return True, None  # restart auto-resume still owns this chat
        return False, marker

    async def _clear_stale_resume_pending(self, key: str, marker: Optional[tuple], sid: str, owner: str) -> bool:
        """CAS-clear a stale marker snapshot; False when it changed or the session disappeared."""
        if marker is None:
            return True
        if not await self.async_session_store.clear_resume_pending(key, expected_marker=marker):
            return False
        logger.info("%s: stale resume_pending cleared for session %s; idle ticker owns continuation", owner, sid)
        return True

    async def _loop_wakeup_fire_one(  # health: allow CC -- ordered wake admission combines strict pins, busy gates, claim CAS, and adapter receipt.
        self, sid: str, state: Any, now: float, warned_no_route: set, profile: Optional[str] = None,
    ) -> None:
        """Inject one due /loop wakeup into its session, applying every deferral rule.

        A persisted started tick is never replayed. Existing ownership fences first prove that
        no live, admitted, queued or restart-recovering turn owns it, then recovery skips it and
        schedules the next tick without pausing the loop.
        """
        from hermes_cli.loops import LoopManager, goal_blocks_loop_tick, self_paced_floor_seconds
        from gateway.wake import automatic_route_available

        if not state.awaiting_response and now < state.next_due_at:
            return

        route = state.route or {}
        platform_name = route.get("platform", "")
        chat_id = route.get("chat_id", "")
        if not platform_name or not chat_id:
            return  # CLI / TUI-owned loop — their own schedulers drive it.
        profile = route.get("profile") or profile
        adapters = self._adapters_for_profile(profile)
        adapter = next((a for p, a in adapters.items() if p.value == platform_name), None)
        if adapter is None:
            return
        source = await asyncio.to_thread(self._build_process_event_source, {
            "session_key": "", "platform": platform_name, "chat_id": chat_id,
            **{k: route.get(k, "") for k in ("chat_type", "thread_id", "user_id", "user_name")},
        })
        if source is None:
            return
        if profile and not getattr(source, "profile", None):
            source.profile = profile
        session_key = None
        with suppress(Exception):
            session_key = self._session_key_for_source(source)
        if not session_key:
            return

        def _turn_owner_exists(include_marker: bool = True) -> bool:
            if self._is_session_running(session_key) or session_key in self._running_agents:
                return True
            if session_key in getattr(adapter, "_active_sessions", {}):
                return True
            with suppress(Exception):
                if self._queue_depth(session_key, adapter=adapter) > 0:
                    return True
            if include_marker:
                with suppress(Exception):
                    entry = self.session_store._entries.get(session_key)
                    if entry is not None and getattr(entry, "resume_pending", False):
                        return True
            return False

        if _turn_owner_exists(include_marker=False):
            return
        # Preserve retired state before compression migration or orphan-tick recovery writes.
        if not await automatic_route_available(self, source):
            return

        # The route may resolve to a different durable session after compression, /resume, a
        # topic rebind or /new. Only compression carries the loop; the others defer untouched.
        try:
            lookup = getattr(self.session_store, "lookup_by_session_key", None)
            current = (
                await self._run_in_executor_with_context(lookup, session_key)
                if lookup is not None else None
            )
        except Exception:
            logger.debug("loop wakeup: session ownership lookup failed", exc_info=True)
            return
        if current is not None and current.session_id != sid:
            # Compression deliberately carries conversation responsibility forward. /resume and
            # topic rebinds are reversible, so the loop waits until the route returns; explicit
            # /new retires it in the reset handler, never here.
            try:
                tip = await self._run_in_executor_with_context(
                    lambda: self.session_store._db_for_key(session_key).get_compression_tip(sid)
                )
            except Exception:
                logger.debug("loop wakeup: compression lineage lookup failed", exc_info=True)
                return
            if tip == current.session_id:
                await self._migrate_compressed_loop(sid, tip)
            return

        entry = None
        with suppress(Exception):
            entry = self.session_store._entries.get(session_key)
        marker_owned, stale_marker = await self._stale_resume_pending_marker(session_key, entry)
        if marker_owned:
            return
        if stale_marker is not None and (
            _turn_owner_exists(include_marker=False)
            or not await self._clear_stale_resume_pending(session_key, stale_marker, sid, "loop wakeup")
        ):
            return

        if state.awaiting_response:
            if _turn_owner_exists():
                return
            outcome = await self._run_in_executor_with_context(
                lambda: LoopManager(session_id=sid).recover_tick(owns_turn=_turn_owner_exists)
            )
            if outcome:
                logger.warning("loop wakeup: %s for session %s; no external effect replayed", outcome, sid)
            return
        if now < state.next_due_at or _turn_owner_exists():
            # The adapter routes pending clarification replies before admission. A wake must
            # never enter that path as a fabricated human answer while another turn owns it.
            return
        def _due_manager():
            from agent.estop import check_paused
            if check_paused("loop", logger):
                return None, False, 0
            mgr = LoopManager(session_id=sid)
            state = mgr.state
            if state is None or not mgr.is_due(now):
                return None, False, 0
            interval = state.interval_seconds or self_paced_floor_seconds()
            return mgr, goal_blocks_loop_tick(sid), interval

        mgr, goal_blocks, interval = await self._run_in_executor_with_context(_due_manager)
        if mgr is None:
            return

        def _log_deferral(reason):
            logged_at = self.__dict__.setdefault("_loop_deferral_logged_at", {})
            log_key = (session_key, sid, reason)
            if now - logged_at.get(log_key, float("-inf")) >= interval:
                logged_at[log_key] = now
                logger.info("loop wakeup: session %s deferred by %s", sid, reason)

        # A runnable goal gets the first interval, not indefinite ownership of an idle session.
        # Live goal turns and continuations still win the turn-owner fences below, at any age.
        if goal_blocks and now - mgr.state.next_due_at < interval:
            _log_deferral("active goal")
            return

        # Loop wakeups are injected as internal events, which bypass the inbound estop gate; without
        # this a `hermes pause` would still start agent turns. Not claiming the tick keeps it due.
        if _turn_owner_exists():
            return
        # fire_tick()/complete_tick() are writes (BEGIN IMMEDIATE) taking the SessionDB writer lock; a slow
        # writer elsewhere holding it while the loop thread blocked froze the gateway until the watchdog
        # fired. The context-preserving executor keeps the profile HERMES_HOME override under multiplex.
        if _turn_owner_exists():
            return
        reservation = self._reserve_background_turn(session_key)
        if reservation is None:
            _log_deferral("full background gate")
            return  # background turn limit reached: the tick stays unclaimed and due
        begin_generation = getattr(self, "_begin_session_run_generation", None)
        if callable(begin_generation):
            generation = begin_generation(session_key)
        else:
            current_generation = getattr(self, "_current_session_run_generation", None)
            generation = current_generation(session_key) if callable(current_generation) else 0
        is_run_current = getattr(self, "_is_session_run_current", None)
        if not callable(is_run_current):
            is_run_current = lambda _key, _generation: True
        wakeup = await self._run_in_executor_with_context(
            functools.partial(
                mgr.fire_tick, generation,
                can_claim=lambda: not _turn_owner_exists() and is_run_current(session_key, generation),
            )
        )
        if not wakeup or _turn_owner_exists() or not is_run_current(session_key, generation):
            self._release_background_turn(reservation)
            # A consumed claim is skipped by recovery after the current owner releases.
            return
        # #85957: after the parent turn's event.complete the CLIENT owns the next turn on this stateless
        # surface. Persist the completion as a durable delivery row — never self-post it as a new role=user
        # prompt.
        # #85957: same client-owns-the-turn rule as the raw-key branch above — persist the completion as a
        # delivery row, never self-post it as a new role=user prompt.
        tick = (mgr.state.created_at, mgr.state.ticks_fired)
        is_command = wakeup.lstrip().startswith("/")
        event = self._synthetic_prompt_event(
            source, wakeup, internal=True, reply_expected=False, goal_continuation=False,
        )
        setattr(event, BACKGROUND_RESERVATION_ATTR, reservation)  # a claimed tick is never refused
        from hermes_cli.session_controls import loop_wake_metadata
        event.metadata.update({
            "gateway_session_key": session_key,
            "gateway_session_id": sid,
            "gateway_session_strict": True,
            # Keep the legacy fields for adapters and standalone hosts that still use the original
            # wake marker while the structured identity is carried alongside them.
            "hermes_loop_wakeup": True,
            "gateway_loop_tick": list(tick),
            **loop_wake_metadata(sid, mgr.state, command=is_command),
        })
        event._gateway_loop_execution_fence = (
            sid, tick, generation, mgr.state.control_generation,
        )
        try:
            logger.info(
                "loop wakeup #%s — injecting for %s chat=%s thread=%s",
                mgr.state.ticks_fired if mgr.state else "?",
                platform_name, source.chat_id, source.thread_id,
            )
            # The tick is already durably started before any adapter/turn preparation. Lightweight
            # native/plugin doubles without the adapter receipt surface retain the direct path.
            from gateway.session_automation import admit_route_wake
            # Authority-backed routes commit to the canonical FIFO; slash commands and legacy
            # adapters retain the fork's strict admission-receipt path.
            if wakeup.lstrip().startswith("/") or not await admit_route_wake(
                self, adapter, source, wakeup, "loop-tick", event=event,
            ):
                from gateway.wake import admit_internal_event
                has_admission_receipt = "_message_handler" in getattr(adapter, "__dict__", {})
                if has_admission_receipt:
                    await admit_internal_event(adapter, event)
                else:
                    # Lightweight/plugin adapters predate the explicit admission-receipt surface, but
                    # they still need the same compression/control fence before handling a wake, and
                    # the same receipt rule as admit_internal_event: MessageEvent defaults the receipt
                    # to False, so a handler that never confirms admission is a refusal and the started
                    # tick is left for skip-only recovery.
                    from hermes_cli.session_controls import internal_wake_is_current
                    if not await self._run_in_executor_with_context(
                        functools.partial(internal_wake_is_current, event)
                    ):
                        raise RuntimeError("loop wake superseded before direct adapter admission")
                    await adapter.handle_message(event)
                    if getattr(event, "_gateway_accepted", False) is not True:
                        raise RuntimeError("loop wake not accepted by direct adapter")
                rebound_sid = (event.metadata or {}).get("loop_wakeup_session_id")
                if rebound_sid and rebound_sid != sid:
                    def _rebound_fence():
                        child = LoopManager(session_id=rebound_sid).state
                        if child is None or (child.created_at, child.ticks_fired) != tick:
                            return None
                        return int(getattr(child, "control_generation", 0) or 0)
                    rebound_control = await self._run_in_executor_with_context(_rebound_fence)
                    if rebound_control is None:
                        raise RuntimeError("loop wake continuation missing its started tick")
                    event._gateway_loop_execution_fence = (
                        rebound_sid, tick, generation, rebound_control,
                    )
                # Native adapters settle command-only ticks at the handled-command boundary;
                # rewriting commands retain their claim until the model-turn hooks run. Legacy
                # direct adapters have no dispatch lifecycle, so preserve their inline settlement.
                if ((not has_admission_receipt or getattr(event, "_gateway_inline_command_handled", False))
                        and is_command
                        and getattr(event, "_gateway_accepted", None) is True):
                    settled_sid = (event.metadata or {}).get("loop_wakeup_session_id", sid)
                    settled_mgr = mgr if settled_sid == sid else LoopManager(session_id=settled_sid)
                    await self._run_in_executor_with_context(
                        functools.partial(settled_mgr.complete_tick, "", tick=tick)
                    )
        except Exception as exc:
            logger.warning("loop wakeup injection failed for %s: %s", sid, exc)
            # Do not refund a started tick after delivery failure. Recovery skips it and schedules
            # the next due time; only a pre-start claim can use the conditional abandon path.
            with suppress(Exception):
                if getattr(mgr.state, "claim_phase", "") == "claimed":
                    await self._run_in_executor_with_context(
                        mgr.abandon_tick, tick=tick, owns_turn=_turn_owner_exists,
                    )
        # Viewers swap the countdown for "running now" (or re-anchor it after a slash tick settled).
        self._publish_local_control(source)

    async def _goal_wakeup_fire_one(self, sid: str) -> None:
        """Resume one idle parked /goal whose wait barrier has lifted.

        The post-turn judge is the only other path that re-evaluates a barrier, and it needs a turn.
        A process killed by a gateway restart, a process started without notify_on_complete, or an
        elapsed timed wait produces no turn, so without this the goal stays parked until an
        unrelated message arrives. The barrier is cleared only after the adapter admitted the
        continuation; a busy or unroutable session keeps it for the next scan."""
        from hermes_cli.goals import GoalManager

        store = getattr(self, "session_store", None)
        entry = await self.async_session_store.lookup_by_session_id(sid) if store is not None else None
        if entry is None or getattr(entry, "origin", None) is None or getattr(entry, "suspended", False):
            return  # no live route (reset, compressed away, CLI/TUI-owned): nothing to wake here
        assert store is not None
        source = self._restored_source(entry)
        adapter = self._delivery_adapter_for(source) if source is not None else None
        if adapter is None or not getattr(adapter, "_message_handler", None):
            return
        key = entry.session_key
        if (self._is_session_running(key) or key in getattr(adapter, "_active_sessions", {})
                or self._queue_depth(key, adapter=adapter) > 0):
            return  # a turn (or restart auto-resume) is in flight; its post-turn judge owns the barrier
        if not self._background_turn_room():
            return  # background turn limit reached: keep the barrier for a later scan

        marker_owned, resume_marker = await self._stale_resume_pending_marker(key, entry)
        if marker_owned:
            return

        max_turns = self._goal_max_turns_from_config()
        min_gap = self._goal_min_continuation_gap_from_config()

        def _check():
            mgr = GoalManager(
                session_id=sid, default_max_turns=max_turns, min_continuation_gap_seconds=min_gap,
            )
            return mgr, mgr.lifted_barrier_prompt()

        mgr, prompt = await self._run_in_executor_with_context(_check)
        if not prompt:
            return
        from gateway.wake import automatic_route_available

        if not await automatic_route_available(self, source):
            return
        # A marker absent from the initial snapshot may be created while the barrier check runs.
        # The live SessionEntry is the generation fence for that narrow race: defer to the next
        # scan without clearing the fresh marker or admitting a continuation under stale fences.
        if resume_marker is None and getattr(entry, "resume_pending", False):
            return
        # The barrier check ran off-loop; a turn, adapter guard, or queued event may have appeared
        # while it was running. Re-check every admission fence before clearing or injecting.
        if (self._is_session_running(key) or key in getattr(adapter, "_active_sessions", {})
                or self._queue_depth(key, adapter=adapter) > 0):
            return
        since = mgr.state.waiting_since
        logger.info("goal wakeup: barrier lifted for session %s (%s); resuming",
                    sid, mgr.state.waiting_reason or mgr.state.waiting_on_session or mgr.state.waiting_on_pid)
        event = self._synthetic_prompt_event(
            source, prompt, reply_expected=False, goal_continuation=True,
            goal_session_id=sid, goal_state=mgr.state,
        )
        event.metadata["gateway_session_key"] = key
        reservation = self._reserve_background_turn(key, event)
        if reservation is None:
            return  # background turn limit reached: keep the barrier for a later scan
        if not await self._clear_stale_resume_pending(key, resume_marker, sid, "goal wakeup"):
            self._release_background_turn(reservation)
            return  # marker changed or the session disappeared; retry on the next scan
        from gateway.wake import WakeNotAccepted, admit_internal_event

        try:
            # A normal return from handle_message is not admission (routing refusal, session-key
            # mismatch); only the adapter's receipt proves a continuation was scheduled.
            await admit_internal_event(adapter, event)
        except WakeNotAccepted:
            logger.info("goal wakeup: continuation for session %s not admitted; barrier kept for retry", sid)
            return
        # The lifted-barrier path dispatches before the continuation turn's evaluator runs. Stamp
        # this accepted wake now so a CONTINUE result cannot immediately dispatch a second one.
        # Admission may rebind the continuation to a compression successor, so stamp the live
        # destination from the event metadata rather than the pre-admission manager.
        accepted_sid = str(event.metadata.get("goal_continuation_session_id") or sid)
        anchor_mgr = mgr if accepted_sid == sid else GoalManager(
            session_id=accepted_sid, default_max_turns=max_turns, min_continuation_gap_seconds=min_gap,
        )
        await self._run_in_executor_with_context(
            lambda: anchor_mgr.mark_automatic_continuation_dispatched(
                expected_instance=event.metadata.get("goal_continuation_instance"),
                expected_fingerprint=event.metadata.get("goal_continuation_fingerprint"),
            )
        )
        # Resuming is silent: the continuation turn itself is the visible signal.
        await self._run_in_executor_with_context(mgr.clear_lifted_wait, since)

    async def _loop_wakeup_watcher(self, interval: float = 15.0) -> None:
        """Fire due /loop wakeups and resume lifted /goal waits for idle gateway sessions: a coarse
        ticker scans persisted loops (SessionDB ``loop:*`` rows) and parked goals (``goal:*`` rows
        with a wait barrier) and injects each prompt via the synthetic-message path.
        Deferrals: session running a turn (FIFO would race the live turn); active non-parked /goal
        (goal owns the idle boundary); no routing metadata (one-time warning).

        One ticker owns both kinds of idle injection so a future generation fence (overlapping
        gateways) has a single admission owner to transfer.

        Multiplex: one gateway-wide task, so ``list_active_loops`` alone reads only the launch home's
        store — a ``/loop`` set from a secondary profile's chat would never fire. Every served
        profile's store is scanned under its own home context, and each admitted hit binds its
        full runtime scope against that profile's adapters."""
        from gateway.run import _async_profile_runtime_scope, _resolve_handoff_watch_scopes
        from gateway.run_idle_gates import profile_has_active_loop, profile_has_parked_goal
        await asyncio.sleep(5)  # let platforms finish connecting
        warned_no_route: set = set()

        def _scope(profile_home):
            # profile_home None = the launch profile's own store; once the process multiplexes it
            # binds its own scope instead of running on ambient env (see _scope_or_null).
            if profile_home is not None:
                return _async_profile_runtime_scope(profile_home)
            from tui_gateway.launch_profile_policy import async_launch_profile_scope_if_multiplexed
            return async_launch_profile_scope_if_multiplexed()

        def _has_idle_work(profile_home) -> bool:
            return profile_has_active_loop(profile_home) or profile_has_parked_goal(profile_home)

        async def _scan_one_store(profile_name: Optional[str], profile_home) -> None:
            from hermes_cli.goals import list_parked_goals
            from hermes_cli.loops import list_active_loops

            from hermes_constants import (
                get_routing_process_hermes_home, reset_hermes_home_override, set_hermes_home_override,
            )

            # Store discovery and admission need only the owning home, not secret hydration or
            # terminal preparation. The admitted adapter/turn binds the full runtime scope.
            home_token = set_hermes_home_override(profile_home or get_routing_process_hermes_home())
            try:
                await self._warm_goals_session_db("loop wakeup")
                active_loops = await self._run_in_executor_with_context(list_active_loops)
                now = time.time()
                for sid, state in active_loops:
                    await self._loop_wakeup_fire_one(sid, state, now, warned_no_route, profile_name)
                parked_goals = await self._run_in_executor_with_context(list_parked_goals)
            finally:
                reset_hermes_home_override(home_token)
            if parked_goals:
                async with _scope(profile_home):
                    for sid, _state in parked_goals:
                        try:
                            await self._goal_wakeup_fire_one(sid)
                        except Exception as exc:
                            logger.warning("goal wakeup failed for %s: %s", sid, exc)

        while self._running:
            try:
                # Multiplex resolution walks the filesystem off-loop; a stalled walk on the loop
                # trips the loop-liveness watchdog (exit 75).
                for profile_name, profile_home in await _resolve_handoff_watch_scopes(self):
                    # Idle gate (run_idle_gates): skip the scope entry when the profile's store holds
                    # no active loop or parked goal. The root scan (None) is unscoped and stays cheap.
                    if profile_home is not None and not await self._run_in_executor_with_context(
                            _has_idle_work, profile_home):
                        continue
                    await _scan_one_store(profile_name, profile_home)
            except Exception as exc:
                logger.debug("loop wakeup watcher error: %s", exc)
            await asyncio.sleep(interval)
