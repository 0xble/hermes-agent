'''Turn-context and delivery methods split from the Gateway turn facade.'''

from __future__ import annotations

from typing import Any, Callable, Optional, TYPE_CHECKING, cast
from contextlib import nullcontext, suppress
import asyncio
import inspect
import logging
import queue
import threading
import time
from agent.i18n import t
from gateway import loop_completion
from gateway.bot_peer_notices import automatic_notices_allowed
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, ProcessingOutcome
from gateway.platforms.event import turn_origin_for_event
from gateway.platforms.helpers import typing_metadata_for_turn
from gateway.response_filters import display_kind_for_event, reply_expected_metadata
from gateway.run_turn import (
    TurnContext, SessionSource, _NO_ATTENDED_ACTIVITY_STAMP, _tool_call_logger,
    _unexpected_silence_reply,
)
from gateway.run_turn_final_reconcile import edit_streamed_message, owns_reconcilable_preview
from gateway.run_turn_pending import _drain_leftover_steer
from gateway.run_turn_stream_delivery import stream_confirmed_final_delivery
from gateway.session_context import bound_turn_origin, scoped_turn_actor, set_turn_origin
from gateway.warning_notifications import diagnostic_metadata, diagnostic_turn_muted
from gateway.zero_cut_handover_drain import handover_runs_followups
from agent.session_activity import format_iteration_progress
from gateway.turn_context import TurnWorker

if TYPE_CHECKING:
    from gateway.run import GatewayRunner
    from gateway.run_turn_runner import TurnRunner

logger = logging.getLogger("gateway.run")

class GatewayTurnExecutionMixin:
    def _run_agent_build_turn_context(
        self, disp: GatewayRunner._RunAgentDisplay, AIAgent: Any, *, message: str, source: SessionSource,
        session_key: Optional[str], run_generation: Optional[int],
        reasoning_override=_NO_ATTENDED_ACTIVITY_STAMP, **turn_params,
    ) -> tuple[TurnContext, TurnRunner, Any]:
        """Build the ``TurnContext`` and its ``TurnRunner``; ``turn_params`` (history, context_prompt,
        session_id, persist_user_*, …) are stored verbatim. Returns ``(turn_ctx, turn_runner,
        cleanup_adapter)``."""
        from gateway.run_turn_runner import TurnRunner
        # Discord voice "verbal ack" on the FIRST tool call (discord.voice_fx.enabled): resolve the
        # guild whose voice connection is bound to this text channel (mirrors DiscordAdapter.play_tts).
        _voice_ack_guild: list[Optional[int]] = [self._voice_ack_guild_for(source)]

        # Auto-cleanup of temporary progress bubbles needs a real ``delete_message`` (getattr on the
        # adapter type, so a fake adapter without it means "can't delete", not a crash).
        _cleanup_progress = bool(
            disp.resolve_display_setting(disp.user_config, disp.platform_key, "cleanup_progress")
        )
        _cleanup_adapter = self._delivery_adapter_for(source) if _cleanup_progress else None
        if _cleanup_adapter is not None and getattr(type(_cleanup_adapter), "delete_message", None) in (
            None, BasePlatformAdapter.delete_message,
        ):
            _cleanup_progress = False
            _cleanup_adapter = None

        _post_delivery_adapter = turn_params.pop("_post_delivery_adapter", None)
        # The one-slot progress/holder containers shared with the callbacks are TurnContext defaults.
        turn_ctx = TurnContext(
            source=source, message=message, AIAgent=AIAgent, session_key=session_key,
            run_generation=run_generation, _cleanup_progress=_cleanup_progress, _cleanup_adapter=_cleanup_adapter,
            _post_delivery_adapter=_post_delivery_adapter,
            _run_still_current=self._run_still_current_fn(session_key, run_generation),
            progress_queue=queue.Queue() if disp.needs_progress_queue else None,
            _voice_ack_guild=_voice_ack_guild, _voice_ack_loop=asyncio.get_running_loop(),
            **{name: getattr(disp, name) for name in self._DISPLAY_TO_TURN_CTX}, **turn_params,
        )
        # Capture assignment synchronously during turn assembly, before yielding to busy edits.
        # Callback-only TurnRunner consumers need no routing state. Config stays live per request.
        state = self._peek_session_state(session_key)
        model_assignment = {
            **self._snapshot_session_model_override(session_key),
            "model_role": state.conversation.model_role if state is not None else None,
            "model_scope": self._session_model_override_scope(session_key),
            "reasoning_override": (self._session_reasoning_override(session_key)
                                   if reasoning_override is _NO_ATTENDED_ACTIVITY_STAMP else reasoning_override),
            "reasoning_scope": state.conversation.reasoning_override_scope if state is not None else "session",
        }
        if _cleanup_progress and session_key:
            turn_ctx._card_recorder = self._progress_card_recorder(turn_ctx)
        turn_runner = TurnRunner(self, turn_ctx, model_assignment=model_assignment)
        turn_ctx.mute_notification_reply = diagnostic_turn_muted(
            turn_ctx.persist_user_display_metadata, source.platform, turn_ctx.user_config)
        # Agent tool-lifecycle callbacks live on the runner (bound methods, same signatures).
        turn_ctx.progress_callback = turn_runner.progress_callback
        turn_ctx.voice_ack_callback = turn_runner.voice_ack_callback
        turn_ctx.native_tool_start_callback = turn_runner.combined_tool_start_callback
        turn_ctx.native_tool_complete_callback = turn_runner.native_tool_complete_callback
        return turn_ctx, turn_runner, _cleanup_adapter

    def _thread_metadata_for_progress(
        self, source: SessionSource, event_message_id: Optional[str], _progress_thread_id: Any,
        _relay_prospective_thread_id: Optional[str],
    ) -> Optional[dict[str, Any]]:
        """Thread metadata for a progress-lane send; relay Discord auto-thread lane falls back to the reply anchor.

        The connector will auto-thread on the reply anchor (thread is born on its FIRST send), so
        carrying it routes progress / status bubbles into the same thread as the final reply."""
        if not _progress_thread_id:
            metadata = None
        elif _progress_thread_id == source.thread_id:
            metadata = self._thread_metadata_for_source(source, event_message_id)
        else:
            metadata = self._thread_metadata_for_target(
                source.platform, source.chat_id, _progress_thread_id,
                chat_type=getattr(source, "chat_type", None), reply_to_message_id=event_message_id,
            )
        if metadata is None and _relay_prospective_thread_id:
            metadata = {"reply_to_message_id": event_message_id}
        return metadata

    def _run_agent_progress_threading(
        self, source: SessionSource, event_message_id: Optional[str], _native_slack_task_cards: bool
    ) -> tuple[Optional[dict], Optional[str], Optional[dict]]:
        """Resolve where progress bubbles are threaded (platform-specific).

        Returns ``(progress_metadata, progress_reply_to, status_thread_metadata)``; the latter is
        for status / approval / stream sends (Feishu topics need the triggering message id via the
        reply API, so carry it as a fallback). Slack and Buzz honour the user's reply_in_thread
        opt-out: never synthesise a thread for progress, or every later reply inherits it."""
        from gateway.run import _non_conversational_metadata, _resolve_progress_thread_id
        is_buzz = str(getattr(source.platform, "value", source.platform) or "").lower() == "buzz"
        _progress_reply_in_thread = True
        _adapter = self._delivery_adapter_for(source) if source.platform == Platform.SLACK or is_buzz else None
        if _adapter is not None:
            try:
                if is_buzz:
                    _progress_reply_in_thread = getattr(_adapter, "_reply_to_mode", "first") != "off"
                else:
                    # Relay lane: the adapter owns mode resolution; native lane: flat extra key.
                    _mode_fn = getattr(_adapter, "_effective_reply_in_thread", None)
                    _progress_reply_in_thread = bool(
                        _mode_fn() if callable(_mode_fn) else _adapter.config.extra.get("reply_in_thread", True)
                    )
            except Exception:
                _progress_reply_in_thread = True
        _progress_thread_id = _resolve_progress_thread_id(
            source.platform, source.thread_id, event_message_id, reply_in_thread=_progress_reply_in_thread,
        )
        # Relay Discord auto-thread lane: the connector stamps prospective_thread_id at ingest.
        _relay_prospective_thread_id = (
            str(getattr(source, "prospective_thread_id", None))
            if source.platform == Platform.DISCORD
            and getattr(source, "delivered_via_upstream_relay", False)
            and getattr(source, "prospective_thread_id", None)
            and not source.thread_id
            else None
        )
        _progress_metadata = _non_conversational_metadata(
            self._thread_metadata_for_progress(
                source, event_message_id, _progress_thread_id, _relay_prospective_thread_id,
            ),
            platform=source.platform,
        )
        if _native_slack_task_cards:
            # chat.startStream in channels requires the recipient team/user pair; harmless elsewhere.
            _progress_metadata = dict(_progress_metadata or {})
            if source.scope_id:
                _progress_metadata.setdefault("recipient_team_id", source.scope_id)
                _progress_metadata.setdefault("slack_team_id", source.scope_id)
            if source.user_id:
                _progress_metadata.setdefault("recipient_user_id", source.user_id)
        # Buzz has no native thread_id: thread via reply-to unless the user opted out.
        _progress_reply_to = (
            event_message_id
            if (source.platform in (Platform.FEISHU, Platform.MATTERMOST) and source.thread_id and event_message_id)
            or (is_buzz and event_message_id and _progress_reply_in_thread)
            or _relay_prospective_thread_id
            else None
        )
        if source.platform == Platform.FEISHU and source.thread_id and event_message_id:
            _status_thread_metadata = {"thread_id": _progress_thread_id, "reply_to_message_id": event_message_id}
        else:
            _status_thread_metadata = self._thread_metadata_for_progress(
                source, event_message_id, _progress_thread_id, _relay_prospective_thread_id,
            )
        return _progress_metadata, _progress_reply_to, _status_thread_metadata

    async def _run_agent_write_tool_log(self, log_queue: Any) -> None:
        """Drain log_queue and append tool-call lines to tool_calls.log (tool_progress=log).

        RotatingFileHandler (5MB × 3) bounds the log; RedactingFormatter keeps secrets off disk."""
        if log_queue is None:
            return
        tool_logger = _tool_call_logger()
        try:
            while True:
                try:
                    tool_logger.info("%s", log_queue.get_nowait())
                except queue.Empty:
                    await asyncio.sleep(0.3)
                except Exception as e:
                    logger.error("write_tool_log error: %s", e)
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            # Drain remaining entries so late tool calls from the final iteration aren't lost.
            with suppress(Exception):
                while True:
                    tool_logger.info("%s", log_queue.get_nowait())
            with suppress(Exception):
                for handler in tool_logger.handlers:
                    handler.flush()

    def _run_agent_start_streaming_tts(
        self, source: SessionSource, message_type: Optional[str],
        _status_thread_metadata: Optional[dict[str, Any]], streaming_tts_consumer_holder: list,
    ) -> None:
        """Start the streaming-TTS consumer for a voice-input turn on an auto-TTS chat.

        Created on the gateway loop thread (not run_sync's executor); an inactive consumer leaves
        the holder None so the whole-file fallback path runs."""
        # Skip when streaming TTS already delivered audio for this turn (#60671).
        # This avoids a cross-scope NameError: the outer interrupt / finalisation paths reference the
        # consumer via ``streaming_tts_consumer_holder[0]``. Gates: voice input, auto-TTS enabled for this
        # chat, adapter supports streaming, and a usable streaming TTS provider configured. See #60671.
        _stts_adapter = self._delivery_adapter_for(source)
        _is_voice_input = (
            message_type is not None
            and str(getattr(message_type, "value", message_type)).lower() == "voice"
        )
        if _stts_adapter is None or not _is_voice_input or not _stts_adapter._should_auto_tts_for_chat(source.chat_id):
            return
        try:
            from gateway.streaming_tts_consumer import StreamingTTSConsumer
            from tools.tts_tool import _load_tts_config
            _stts_consumer = StreamingTTSConsumer(
                adapter=_stts_adapter, chat_id=source.chat_id, tts_config=_load_tts_config(),
                loop=self._gateway_loop or asyncio.get_event_loop(),
                metadata=_status_thread_metadata,
            )
            if _stts_consumer.active:
                streaming_tts_consumer_holder[0] = _stts_consumer
                _stts_consumer.start()
        except Exception as _stts_err:
            logger.debug("Could not set up streaming TTS consumer: %s", _stts_err)

    async def _run_agent_stream_consumer_task(self, stream_consumer_holder: list) -> None:
        """Wait (up to 10s) for the stream consumer to be created inside run_sync, then run it."""
        for _ in range(200):
            if stream_consumer_holder[0] is not None:
                await stream_consumer_holder[0].run()
                return
            await asyncio.sleep(0.05)

    @staticmethod
    async def _await_stream_task(stream_task) -> None:
        """Give the stream consumer task 5s to flush, then cancel it."""
        try:
            await asyncio.wait_for(stream_task, timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            stream_task.cancel()
            with suppress(asyncio.CancelledError):
                await stream_task

    async def _run_agent_track_agent(self, turn_ctx: TurnContext) -> None:
        """Track this agent as running for the session (interrupt support) once it is created — only
        if this run is still current, else leave the newer run's slot alone."""
        session_key, run_generation, agent_holder = turn_ctx.session_key, turn_ctx.run_generation, turn_ctx.agent_holder
        while agent_holder[0] is None:
            await asyncio.sleep(0.05)
        if not session_key:
            return
        if run_generation is not None and not self._is_session_run_current(session_key, run_generation):
            logger.info(
                "Skipping stale agent promotion for %s — generation %s is no longer current",
                session_key or "", run_generation,
            )
            return
        turn_state = self._session_state(session_key).turn
        turn_state.agent, turn_state.ctx = agent_holder[0], turn_ctx
        if self._draining:
            self._update_runtime_status("draining")

    async def _run_agent_fire_pending_interrupt(
        self, adapter: Any, agent: Any, source: SessionSource, session_key: str,
        _interrupt_detected: asyncio.Event, streaming_tts_consumer_holder: list, *,
        log_context: str, log: Callable[[], None],
    ) -> None:
        """Peek the adapter's pending event, then signal the agent + abort streaming TTS.

        Peek WITHOUT consuming: the event must stay for the post-run ``_dequeue_pending_event()``
        (popping races the agent finishing), which also transcribes a voice note, so the signal
        never waits for STT (#143961)."""
        _peek_event = adapter._pending_messages.get(session_key)
        pending_text = None if _peek_event is None else (self._busy_interrupt_text(_peek_event) or "")
        log()
        agent.interrupt(pending_text)
        _interrupt_detected.set()
        # Abort streaming TTS on barge-in.
        # See #60671.
        # See #60671.
        # See #60671.
        # Finalize the streaming-TTS consumer (#60671). finish() is called from the outer event-loop thread
        # (not the executor worker) so early returns from run_sync are also finalised.  wait_complete()
        # drains queued audio; on timeout the consumer is aborted unconditionally — if audio was audible,
        # suppression is preserved so the gateway does not replay from the beginning; if no audio was
        # audible, the whole-file fallback path is permitted.
        _stts = streaming_tts_consumer_holder[0]
        if _stts is not None:
            _stts.abort("barge-in")

    async def _run_agent_monitor_for_interrupt(self, turn_ctx: TurnContext, _interrupt_detected: asyncio.Event) -> None:
        """Poll the adapter for interrupts (new messages) every 200ms and signal the agent.

        Level 1 (base.py) catches regular text before _handle_message(); the inactivity poll loop
        has a BACKUP check in case this task dies. Keyed by session_key, NOT source.chat_id."""
        source, session_key, agent_holder = turn_ctx.source, turn_ctx.session_key, turn_ctx.agent_holder
        streaming_tts_consumer_holder = turn_ctx.streaming_tts_consumer_holder
        if not session_key:
            return
        while True:
            await asyncio.sleep(0.2)
            try:
                # Re-resolve the adapter each iteration so reconnects don't leave a stale reference.
                _adapter = self._delivery_adapter_for(source)
                if not _adapter:
                    continue
                if hasattr(_adapter, 'has_pending_interrupt') and _adapter.has_pending_interrupt(session_key):
                    agent = agent_holder[0]
                    if agent:
                        await self._run_agent_fire_pending_interrupt(
                            _adapter, agent, source, session_key, _interrupt_detected,
                            streaming_tts_consumer_holder, log_context="Voice-interrupt",
                            log=lambda: logger.debug("Interrupt detected from adapter, signaling agent..."),
                        )
                        break
            except asyncio.CancelledError:
                raise
            except Exception as _mon_err:
                logger.debug("monitor_for_interrupt error (will retry): %s", _mon_err)

    async def _run_agent_backup_interrupt_check(
        self, turn_ctx: TurnContext, _interrupt_detected: asyncio.Event, interrupt_monitor: asyncio.Task,
    ) -> None:
        """Backup interrupt check: if the monitor task died or missed the interrupt, catch it here."""
        source, session_key = turn_ctx.source, turn_ctx.session_key
        if _interrupt_detected.is_set() or not session_key:
            return
        _backup_adapter = self._delivery_adapter_for(source)
        _backup_agent = turn_ctx.agent_holder[0]
        if (_backup_adapter and _backup_agent
                and hasattr(_backup_adapter, 'has_pending_interrupt')
                and _backup_adapter.has_pending_interrupt(session_key)):
            await self._run_agent_fire_pending_interrupt(
                _backup_adapter, _backup_agent, source, session_key, _interrupt_detected,
                turn_ctx.streaming_tts_consumer_holder,
                log_context="Voice-backup-interrupt",
                log=lambda: logger.info(
                    "Backup interrupt detected for session %s (monitor task state: %s)",
                    session_key, "done" if interrupt_monitor.done() else "running",
                ),
            )

    _run_agent_stream_confirmed_final_delivery = staticmethod(stream_confirmed_final_delivery)

    def _run_agent_start_turn_worker(self, turn_ctx: TurnContext, run_sync: Callable[[], Any]) -> TurnWorker:
        """Schedule ``run_sync`` on the executor plus the inactivity watchdog thread.

        *Inactivity* timeout (agent.gateway_timeout / HERMES_AGENT_TIMEOUT, env wins; 0 = unlimited),
        not wall-clock. The daemon watchdog is independent of asyncio: cgroup memory reclaim can
        starve the loop that runs the normal timeout poll."""
        from gateway.run import _float_env, _watch_gateway_turn_inactivity
        from tools.process_registry import process_registry
        agent_holder, session_key, run_generation = turn_ctx.agent_holder, turn_ctx.session_key, turn_ctx.run_generation
        _agent_timeout, _agent_warning = (
            v if v > 0 else None
            for v in (_float_env("HERMES_AGENT_TIMEOUT", 1800), _float_env("HERMES_AGENT_TIMEOUT_WARNING", 900))
        )

        # background=true processes survive a turn: reap only children created by THIS turn on timeout.
        _turn_task_id = turn_ctx.session_id or ""
        # The daemon watchdog is independent of asyncio: cgroup memory reclaim may starve the event loop
        # that runs the normal timeout poll, but it need not also postpone cleanup until the loop recovers
        # (#76115).
        _turn_process_baseline = process_registry.snapshot_running_ids(_turn_task_id)
        turn_ctx.process_task_id = _turn_task_id
        turn_ctx.process_baseline = _turn_process_baseline
        # task_id is session-scoped: gate the reap on this claim still being current so a replacement
        # turn's fresh process isn't killed by this turn's stale baseline.
        worker = TurnWorker(
            agent_timeout=_agent_timeout, agent_warning=_agent_warning, task_id=_turn_task_id,
            process_baseline=_turn_process_baseline, worker_done=threading.Event(),
            turn_done=threading.Event(), timeout_fired=threading.Event(), cleanup_lock=threading.Lock(),
            is_current=(
                (lambda: self._is_session_run_current(session_key, run_generation))
                if run_generation is not None
                else (lambda: True)
            ),
        )
        turn_ctx._worker = worker

        def _run_sync_with_timeout_lifecycle():
            try:
                return run_sync()
            finally:
                # `.turn.agent` stays reachable until the *next* turn is claimed; clearing the
                # ownership markers now means a /stop on the finished turn no longer reaps background
                # work it left running.
                # `.turn.agent` on the session state is only reset to _AGENT_PENDING_SENTINEL when the
                # *next* turn is claimed (see _session_state(...).turn.agent = ... at claim time), so a
                # stale reference to this exact agent instance stays reachable from
                # _interrupt_and_clear_session() until then. See #76115.
                _finished_agent = agent_holder[0] if agent_holder else None
                if _finished_agent is not None:
                    _finished_agent._gateway_turn_process_task_id = ""
                    _finished_agent._gateway_turn_process_baseline = frozenset()
                worker.worker_done.set()
                self._release_finished_turn_agent(turn_ctx, worker)

        if _agent_timeout is not None:
            threading.Thread(
                target=_watch_gateway_turn_inactivity,
                kwargs={
                    "agent_holder": agent_holder, "timeout": _agent_timeout, "poll_interval": 5.0,
                    **self._reaper_kwargs(worker),
                },
                name=f"gateway-turn-watchdog-{_turn_task_id[:12]}",
                daemon=True,
            ).start()
        from gateway.session_runtime_workers import start_turn_worker
        start_turn_worker(self, worker, agent_holder, _run_sync_with_timeout_lifecycle)
        return worker

    @staticmethod
    def _reaper_kwargs(worker: TurnWorker) -> dict:
        """Shared kwargs of the watchdog + timeout-reaper threads."""
        return {
            **{k: getattr(worker, k) for k in ("task_id", "process_baseline", "worker_done", "timeout_fired", "cleanup_lock")},
            "is_still_current": worker.is_current,
        }

    @staticmethod
    def _agent_activity_summary(agent: Any) -> dict:
        """``agent.get_activity_summary()`` or ``{}`` when unavailable / failing."""
        if agent and hasattr(agent, "get_activity_summary"):
            with suppress(Exception):
                return agent.get_activity_summary()
        return {}

    async def _run_agent_inactivity_warning(self, worker, source, _status_thread_metadata) -> None:
        """Staged one-shot warning before the inactivity timeout escalates."""
        from gateway.run import _interim_metadata
        _warn_adapter = self._delivery_adapter_for(source) if automatic_notices_allowed(source) else None
        if not _warn_adapter:
            return
        try:
            await _warn_adapter.emit_warning(
                source.chat_id, t(
                    "gateway.progress.stuck_warning",
                    minutes=int(worker.agent_warning // 60) or 1,
                    remaining=int((worker.agent_timeout - worker.agent_warning) // 60) or 1),
                metadata=_interim_metadata(_status_thread_metadata), logical_platform=source.platform,
            )
        except Exception as _warn_err:
            logger.debug("Inactivity warning send error: %s", _warn_err)

    def _run_agent_timeout_result(self, worker, turn_ctx: TurnContext) -> dict:
        """Synthetic failed run dict for an inactivity timeout, with the activity-tracker diagnostic;
        interrupts the agent if it is still running so the thread pool worker is freed."""
        from gateway.run import _INTERRUPT_REASON_TIMEOUT, _INTERRUPT_TOOL_REASON_TIMEOUT, request_hard_interrupt
        from gateway.session_results import record_terminal_failure
        session_key, result_holder, tools_holder = turn_ctx.session_key, turn_ctx.result_holder, turn_ctx.tools_holder
        _timed_out_agent = turn_ctx.agent_holder[0]
        _activity = self._agent_activity_summary(_timed_out_agent)
        _last_desc = _activity.get("last_activity_desc", "unknown")
        _secs_ago = _activity.get("seconds_since_activity", 0)
        _cur_tool = _activity.get("current_tool")
        _iter_n = _activity.get("api_call_count", 0)
        _iter_max = _activity.get("max_iterations", 0)
        # Operator-facing log keeps the raw resolved value; only the user-facing lines hide the sentinel.
        logger.error(
            "Agent idle for %.0fs (timeout %.0fs) in session %s "
            "| last_activity=%s | iteration=%s/%s | tool=%s",
            _secs_ago, worker.agent_timeout, session_key, _last_desc, _iter_n, _iter_max,
            _cur_tool or "none",
        )
        if _timed_out_agent:
            request_hard_interrupt(_timed_out_agent, _INTERRUPT_REASON_TIMEOUT,
                                   tool_reason=_INTERRUPT_TOOL_REASON_TIMEOUT, delegation_reason="timeout")
        _timeout_mins = int(worker.agent_timeout // 60) or 1
        _iter_progress = format_iteration_progress(_iter_n, _iter_max)
        _diag_lines = [t("gateway.progress.timeout_header", minutes=_timeout_mins)]
        if _cur_tool:
            _diag_lines.append(t(
                "gateway.progress.timeout_stuck_tool",
                tool=_cur_tool, seconds=f"{_secs_ago:.0f}", progress=_iter_progress))
        else:
            _diag_lines.append(t(
                "gateway.progress.timeout_last_activity",
                activity=_last_desc, seconds=f"{_secs_ago:.0f}", progress=_iter_progress))
        _diag_lines.append(t("gateway.progress.timeout_hint"))
        return record_terminal_failure({
            "final_response": "\n".join(_diag_lines),
            "messages": result_holder[0].get("messages", []) if result_holder[0] else [],
            "api_calls": _iter_n,
            "tools": tools_holder[0] or [],
            "history_offset": 0,
            "failed": True,
        })

    async def _run_agent_await_turn_worker(
        self, worker: TurnWorker, turn_ctx: TurnContext,
        _interrupt_detected: asyncio.Event, interrupt_monitor: asyncio.Task,
    ) -> Any:
        """Poll the executor future (inactivity timeout + backup interrupt checks); return its result,
        or a synthetic failed run dict on inactivity timeout. Polls even with an unlimited timeout
        so the backup interrupt check runs if monitor_for_interrupt() silently died."""
        from gateway.run import _abandon_timed_out_gateway_turn
        agent_holder = turn_ctx.agent_holder
        _warning_fired = False
        while True:
            done, _ = await asyncio.wait({worker.executor_task}, timeout=5.0)
            if done:
                # Prefer the real result even if the watchdog fired in the same window (the run already
                # persisted its reply).
                return worker.executor_task.result()
            if worker.agent_timeout is not None:
                if worker.timeout_fired.is_set():
                    break
                _idle_secs = turn_ctx.awake_idle.measure(self._agent_activity_summary(agent_holder[0]).get("seconds_since_activity", 0.0))
                if (not _warning_fired and worker.agent_warning is not None
                        and _idle_secs >= worker.agent_warning
                        and turn_ctx.persist_user_display_kind != "supervisor_checkin"):
                    _warning_fired = True
                    await self._run_agent_inactivity_warning(worker, turn_ctx.source, turn_ctx._status_thread_metadata)
                if _idle_secs >= worker.agent_timeout:
                    threading.Thread(
                        target=_abandon_timed_out_gateway_turn,
                        kwargs={"agent_holder": agent_holder, **self._reaper_kwargs(worker)},
                        name=f"gateway-turn-reaper-{worker.task_id[:12]}", daemon=True,
                    ).start()
                    break
            await self._run_agent_backup_interrupt_check(turn_ctx, _interrupt_detected, interrupt_monitor)
        return self._run_agent_timeout_result(worker, turn_ctx)

    def _run_agent_evict_on_fallback(self, turn_ctx: TurnContext) -> None:
        """Evict the cached agent when a fallback model activated on a SUCCESSFUL run (so /model shows
        the active model and the next message retries the primary). Skip failed runs: evicting
        would loop bad model → fallback → evict → recreate."""
        session_key = turn_ctx.session_key
        _agent = turn_ctx.agent_holder[0]
        _result_for_fb = turn_ctx.result_holder[0]
        if _agent is None or not hasattr(_agent, 'model') or (_result_for_fb and _result_for_fb.get("failed")):
            return
        # A provider fallback is drift even when it serves the configured model name on another endpoint.
        if getattr(_agent, "_provider_fallback_active", False) is not True:
            # A LOCAL session runs its frozen launch policy's model, not the gateway's configured one.
            from gateway.session_authorities import active_authority
            from gateway.session_policy import policy_for_source, turn_model
            policy = policy_for_source(self, turn_ctx.source)
            _cfg_model = self._fallback_baseline_model(
                session_key, turn_ctx.source, _agent,
                policy_model=turn_model(policy, active_authority(self)) if policy and policy.model else None,
            )
            if _agent.model == _cfg_model or self._is_intentional_model_switch(session_key, _agent, _cfg_model):
                return
        # Keep the evicted parent identifiable so its still-running children remain visible to the
        # replacement session, but only when the fallback eviction actually belongs to this route.
        _agent._evicted_on_fallback = session_key or True
        self._evict_cached_agent(session_key)

    async def _run_agent_finalize_streaming_tts(self, turn_ctx: TurnContext, adapter: Any) -> None:
        """Finalize the streaming-TTS consumer on the outer event-loop thread (covers early returns
        from run_sync). On drain timeout abort to free the task — audible streams keep whole-file
        suppression, silent streams stay eligible for the whole-file fallback."""
        _stts = turn_ctx.streaming_tts_consumer_holder[0]
        if _stts is None:
            return
        _stts.finish()
        try:
            await _stts.wait_complete(timeout=10.0)
        except Exception as _stts_done_err:
            logger.debug("streaming TTS wait_complete error: %s", _stts_done_err)
        if not _stts.done:
            _stts.abort("streaming TTS finalisation timeout")
            await _stts.wait_complete(timeout=2.0)
        if _stts.suppress_whole_file and adapter is not None:
            _mark_turn = getattr(adapter, "_mark_streaming_tts_completed_turn", None)
            if callable(_mark_turn):
                _mark_turn(turn_ctx.session_key, turn_ctx.run_generation)

    async def _run_agent_drain_pending(
        self, result: Any, adapter: Any, source: SessionSource, session_key: Optional[str]
    ) -> tuple[Any, Optional[str]]:
        """Dequeue the adapter's pending / interrupt / leftover-steer follow-up as ``(pending_event, pending)``.

        Keyed by session_key (not source.chat_id) to match the adapter's storage keys."""
        from gateway.run import _build_media_placeholder, _dequeue_pending_event, _is_control_interrupt_message
        from gateway.session_ingress import executing_admission
        pending_event = None
        pending = None
        if result and adapter and session_key:
            pending_event = _dequeue_pending_event(adapter, session_key)
            if result.get("compression_exhausted") and pending_event is not None:
                self._defer_past_exhaustion_reset(result, adapter, source, session_key, pending_event)
                return None, None
            # /queue overflow: promote the next queued event into the consumed "next-up" slot so the
            # recursive drain sees it (keeps FIFO order; a mid-chain /queue can't jump the queue).
            pending_event = self._promote_queued_event(session_key, adapter, pending_event)
            if result.get("interrupted") and not pending_event and result.get("interrupt_message"):
                interrupt_message = result.get("interrupt_message")
                if result.get("interrupt_source") or _is_control_interrupt_message(interrupt_message):
                    logger.info(
                        "Ignoring control interrupt message for session %s: %s (source=%s)",
                        session_key or "?", interrupt_message, result.get("interrupt_source"),
                    )
                else:
                    # FIFO already has this send.
                    pending = None if executing_admission.get() else interrupt_message
            elif pending_event:
                # Transcribe audio BEFORE it becomes the next user turn (real transcript, not a path).
                _pending_text = pending_event.text or ""
                if self._pending_event_audio_paths(pending_event):
                    pending, _ = await self._transcribe_and_echo_pending_voice(
                        pending_event, adapter, source, _pending_text, log_context="Voice-drain",
                        metadata={"thread_id": source.thread_id} if source.thread_id else None,
                    )
                    pending = pending or _build_media_placeholder(pending_event)
                else:
                    pending = _pending_text or _build_media_placeholder(pending_event)
                if pending:
                    logger.debug("Processing queued message after agent completion: '%s...'", pending[:40])

        pending_event, pending = _drain_leftover_steer(
            self, result, adapter, source, session_key, pending_event, pending, logger=logger,
        )

        # Safety net: a pending slash command is never passed to the agent as user input.
        if pending and pending.strip().startswith("/"):
            _pending_cmd_word = pending.strip().split(None, 1)[0][1:].lower()
            if _pending_cmd_word:
                with suppress(Exception):
                    from hermes_cli.commands import resolve_command as _rc_pending
                    if _rc_pending(_pending_cmd_word):
                        logger.info(
                            "Discarding command '/%s' from pending queue — "
                            "commands must not be passed as agent input", _pending_cmd_word,
                        )
                        pending_event = None
                        pending = None

        if self._draining and (pending_event or pending) and not handover_runs_followups(self) and getattr(pending_event, "_admitted_before_drain", False) is not True:
            # The adapter slot was already dequeued; spool it so startup recovery can replay it.
            from gateway.shutdown_flush import flush_pending_to_file
            # Media-only events can have no text even after transcription; persist the resolved
            # user-turn placeholder instead of writing an unrecoverable empty-text event.  A
            # runner-level interrupt can be only a string, so bind it back to this turn's source
            # before spooling; otherwise _serialise_value has no author or transport identity to
            # preserve and restart recovery must decline it as an unsafe shared-chat input.
            spool_value = pending_event if pending_event and pending_event.text else pending
            # Only a non-shared session (DM or per-user key) has a single participant who must
            # be the author. In a shared session another participant may have sent the leftover,
            # so leave it unauthored: recovery then declines it visibly instead of running it as
            # the wrong user.
            _bind_turn_author = (isinstance(source, SessionSource)
                                 and not self._is_shared_session_source(source))
            if isinstance(spool_value, str):
                import uuid
                from gateway.platforms.base import MessageEvent
                from gateway.session_identity import replace_source
                # A leftover /steer or interrupt text has no transport id of its own (reusing the
                # turn's message id would make recovery mistake it for the answered predecessor),
                # so give it the recovery-only dedupe key internal follow-ups already use.
                spool_source = source if _bind_turn_author or not isinstance(source, SessionSource) else (
                    replace_source(source, user_id=None, user_name=None, user_id_alt=None))
                spool_value = MessageEvent(text=spool_value, source=spool_source,
                                           user_id=getattr(spool_source, "user_id", None))
                spool_value._hermes_recovery_id = f"hermes-recovery-{uuid.uuid4().hex}"
            elif (spool_value is not None and _bind_turn_author
                    and not getattr(spool_value, "internal", False)):
                event_source = getattr(spool_value, "source", None) or source
                if (isinstance(event_source, SessionSource) and not event_source.user_id
                        and getattr(source, "user_id", None)):
                    from gateway.session_identity import replace_source
                    spool_value.source = replace_source(event_source, user_id=source.user_id)
            try:
                preserved = flush_pending_to_file({session_key or "": spool_value}, reason="shutdown_follow_up")
            except Exception:
                logger.exception("Failed to preserve pending follow-up for session %s during gateway shutdown",
                                 session_key)
            else:
                if preserved:
                    logger.info("Preserved pending follow-up for session %s during gateway shutdown", session_key)
                else:
                    logger.error("Failed to preserve pending follow-up for session %s during gateway shutdown",
                                 session_key)
            pending_event = None
            pending = None
        if self._draining and result and adapter and session_key and pending_event is None and not handover_runs_followups(self):
            pending_event = self._take_wake_admitted_before_drain(session_key, adapter)
            pending = (pending_event.text or _build_media_placeholder(pending_event)) if pending_event is not None else None
        return pending_event, pending

    async def _run_agent_deliver_first_response(
        self, turn_ctx: TurnContext, adapter: Any, response: Any, result: Any, stream_task: Any,
    ) -> bool:
        """Deliver the first response before a queued follow-up; report confirmed delivery."""
        if turn_ctx.mute_notification_reply:
            return True
        session_key = turn_ctx.session_key
        _sc = turn_ctx.stream_consumer_holder[0]
        if _sc and stream_task:
            try:
                await self._await_stream_task(stream_task)
            except Exception as e:
                logger.debug("Stream consumer wait before queued message failed: %s", e)
        # Delivery uses the finalized task result (empty/failure normalization), not raw ``result``.
        _delivery_result = response if isinstance(response, dict) else (result or {})
        from gateway.run import _sanitize_gateway_final_response
        first_response = _sanitize_gateway_final_response(
            turn_ctx.source.platform, _delivery_result.get("final_response", ""),
            interrupted=bool(_delivery_result.get("interrupted") or (result or {}).get("interrupted")),
        )
        # LOOP_COMPLETE is /loop control text: this lane sends before the normal completion
        # filter runs, so strip it here too. Loop detection reads the untouched result dict.
        from gateway.response_filters import strip_trailing_loop_complete_marker
        first_response = strip_trailing_loop_complete_marker(first_response)
        _already_streamed = self._run_agent_stream_confirmed_final_delivery(
            _sc, first_response, previewed=bool(_delivery_result.get("response_previewed")),
        )
        # Same silence predicate as the normal path, else this branch leaks the literal marker.
        _silent = False
        if self._is_intentional_silence(_delivery_result, first_response):
            # Runtime provenance describes the continuation row, not the original human's silence
            # contract. A queued successor must not make the retry's second marker silently legal.
            silence_kind = (None if getattr(turn_ctx, "silence_retry", False) else _delivery_result.get(
                "queued_terminal_display_kind", turn_ctx.persist_user_display_kind))
            _silence_reply_expected = _delivery_result.get("queued_terminal_reply_expected", turn_ctx.reply_expected)
            from gateway.response_filters import reaction_answered
            if ((reaction_answered(_delivery_result) and silence_kind != "supervisor_checkin")
                    or self._silence_allowed_for_source(
                        turn_ctx.source, silence_kind, turn_ctx.reply_expected, _delivery_result)):
                logger.info(
                    "Queued follow-up for session %s: suppressing intentional silence marker before continuing.",
                    session_key or "?",
                )
                first_response = ""
            else:
                logger.warning(
                    "Queued follow-up for session %s: replacing a human-turn silence marker.",
                    session_key or "?",
                )
                first_response = _unexpected_silence_reply()
                _already_streamed = False
        await self._record_room_silence_decision(
            session_key, _delivery_result, _silent,
            display_kind=turn_ctx.persist_user_display_kind, reply_expected=turn_ctx.reply_expected)
        # Failed turns deliver their text but never their attachments (completed-turn parity).
        _deliver_media = not _delivery_result.get("failed")
        if first_response:
            logger.info(
                "Queued follow-up for session %s: final text delivery confirmed; delivering explicit media before continuing."
                if _already_streamed else
                "Queued follow-up for session %s: final stream delivery not confirmed; sending first response before continuing.",
                session_key or "?",
            )
            try:
                from gateway.outbound_reply import ensure_reply_uid
                _queued_reply_uid = ensure_reply_uid(result)
                _text_delivered = await self._deliver_queued_first_response(
                    first_response, source=turn_ctx.source, adapter=adapter,
                    metadata=turn_ctx._status_thread_metadata,
                    event_message_id=turn_ctx.event_message_id,
                    transcript_message_uid=_queued_reply_uid,
                    text_already_delivered=_already_streamed,
                    deliver_media=_deliver_media, stream_consumer=_sc,
                    # The text send records a delivery-ledger obligation under this key, keyed on
                    # the raw inbound id (the anchor above is only the reply target).
                    session_key=session_key, inbound_message_id=turn_ctx.inbound_message_id,
                )
            except Exception as e:
                logger.warning("Failed to send first response before queued message: %s", e)
                return False
            else:
                # One source of truth for "this turn's final already reached the chat": the normal
                # completion path (`_hmwa_deliver_turn_response`) consults ``already_sent`` on the
                # result the queued lane hands back. Every early `return result` after this point
                # (follow-up text refused, stale goal continuation) otherwise re-sends the text the
                # fallback just delivered — the #81052 duplicate. A REFUSED send reports False, and
                # the completion send stays the fallback so the user is not left with nothing.
                if _text_delivered and isinstance(result, dict):
                    result["already_sent"] = True
                    # The queued lane already uploaded this response's MEDIA: attachments; without
                    # this the completion path's already_sent rescan uploads every file twice.
                    result["media_already_delivered"] = _deliver_media
                    # Its copy blocks went out (or wait in the ledger) on that lane too; the
                    # completion path must not send them a second time.
                    result["copy_already_delivered"] = True
                return bool(_text_delivered)
        return True

    def _queued_followup_at_depth_cap(
        self, turn_ctx: TurnContext, adapter: Any, pending: Optional[str], pending_event: Any,
        source: SessionSource, session_key: Optional[str], response: Any, history: list[dict[str, Any]],
    ) -> Any:
        """Queue rather than recurse once repeated interrupts reach the safety cap."""
        logger.warning(
            "Interrupt recursion depth %d reached for session %s — queueing message instead of recursing.",
            turn_ctx._interrupt_depth, session_key,
        )
        adapter = self._delivery_adapter_for(source)
        if adapter and session_key and hasattr(adapter, "_pending_messages"):
            self._requeue_at_depth_cap(session_key, source, pending_event, pending)
            if pending_event is not None:
                pending_event._gateway_accepted = True
        elif adapter and hasattr(adapter, "queue_message"):
            try:
                adapter.queue_message(
                    session_key, pending, reply_expected=turn_ctx.reply_expected,
                    internal=turn_ctx.internal, metadata=dict(turn_ctx.event_metadata or {}),
                )
            except TypeError:
                adapter.queue_message(session_key, pending)
        return turn_ctx.result_holder[0] or {"final_response": response, "messages": history}

    async def _deliver_queued_followup_response(
        self, turn_ctx: TurnContext, adapter: Any, pending: Optional[str], pending_event: Any,
        response: Any, result: Any, stream_task: Any, source: SessionSource,
        session_key: Optional[str], history: list[dict[str, Any]],
    ) -> Any:
        """Deliver the completed turn before admitting the queued follow-up."""
        self._run_agent_schedule_bubble_cleanup(response, turn_ctx._cleanup_adapter, turn_ctx)
        delivered = await self._run_agent_deliver_first_response(turn_ctx, adapter, response, result, stream_task)
        if not delivered:
            from gateway.platforms.base import MessageEvent, MessageType
            deferred = pending_event or MessageEvent(
                text=str(pending), message_type=MessageType.TEXT, source=source,
                internal=turn_ctx.internal, reply_expected=turn_ctx.reply_expected,
                metadata=dict(turn_ctx.event_metadata or {}),
            )
            if adapter and hasattr(adapter, "_pending_messages"):
                existing = adapter._pending_messages.get(session_key)
                if existing is not None:
                    self._session_state(session_key).conversation.queued_events.insert(0, existing)  # type: ignore[attr-defined]
                adapter._pending_messages[session_key] = deferred
            logger.warning("Queued follow-up deferred after failed first delivery for %s", session_key)
            return result or {"final_response": response, "messages": history}
        owner = turn_ctx._post_delivery_adapter or adapter
        callback = self._pop_post_delivery_callback(owner, session_key, turn_ctx.run_generation)  # type: ignore[attr-defined]
        if callable(callback):
            with suppress(Exception):
                callback_result = callback()
                if inspect.isawaitable(callback_result):
                    await callback_result
        if pending_event is not None:
            from gateway.platforms.base import release_temporary_notices
            await release_temporary_notices(pending_event)
        return None

    async def _prepare_queued_followup_inputs(
        self, turn_ctx: TurnContext, pending: Optional[str], pending_event: Any,
        response: Any, result: Any, source: SessionSource, session_id: str, session_key: str,
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Validate and shape the next queued event before recursive execution."""
        updated_history = result.get("messages", history)
        next_source, next_message, next_session_key = source, pending, session_key
        next_message_id = next_channel_prompt = next_message_type = None
        next_inbound_id = next_persist_message = None
        next_display_kind = display_kind_for_event(pending_event)
        next_reply_expected = pending_event.reply_expected if pending_event is not None else None
        if (
            pending_event is not None
            and isinstance(getattr(pending_event, "metadata", None), dict)
            and pending_event.metadata.get("goal_continuation")
            and next_reply_expected is not True
        ):
            next_reply_expected = False
        if pending_event is not None:
            next_source = getattr(pending_event, "source", None) or source
            if not await getattr(self, "_strict_session_pin_holds")(pending_event, session_id):
                return None
            from gateway.platforms.event import is_goal_continuation_event
            if is_goal_continuation_event(pending_event):
                from hermes_cli.session_controls import refresh_goal_continuation
                if await getattr(self, "_run_in_executor_with_context")(refresh_goal_continuation, pending_event, session_id) is None:
                    logger.info("Discarding stale goal continuation for session %s", session_key or "?")
                    return None
            from hermes_cli.session_controls import LOOP_WAKE_METADATA_KEY, loop_wake_is_current
            if (getattr(pending_event, "metadata", None) or {}).get(LOOP_WAKE_METADATA_KEY) and not await (
                    getattr(self, "_run_in_executor_with_context")(loop_wake_is_current, pending_event.metadata, session_id)):
                logger.info("Discarding superseded loop wake for session %s", session_key or "?")
                return None
            try:
                next_session_key = self._session_key_for_source(next_source)
            except Exception:
                logger.debug("Queued follow-up session-key resolution failed; reusing %s", session_key or "?", exc_info=True)
            next_message = await self._prepare_profile_scoped_inbound_message_text(
                event=pending_event, source=next_source, history=updated_history, session_key=next_session_key,
            )
            if next_message is None:
                return None
            from gateway.run_inbound import strip_discord_triggering_note
            next_persist_message = strip_discord_triggering_note(pending_event, next_message)
            next_message_id = self._reply_anchor_for_event(pending_event)
            next_inbound_id = str(pending_event.message_id) if getattr(pending_event, "message_id", None) else None
            next_channel_prompt, next_source = self._pinned_channel_inputs(
                next_session_key, pending_event.channel_prompt, next_source,
                internal=pending_event.internal or getattr(pending_event, "inherit_channel_pin", False),
            )
            if not pending_event.internal:
                await self._persist_prompt_pins(next_session_key, session_id)
            next_message_type = getattr(pending_event, "message_type", None)
        else:
            next_channel_prompt = turn_ctx.channel_prompt
        return {
            "updated_history": updated_history, "next_source": next_source, "next_message": next_message,
            "next_session_key": next_session_key, "next_message_id": next_message_id,
            "next_inbound_id": next_inbound_id, "next_channel_prompt": next_channel_prompt,
            "next_message_type": next_message_type, "next_persist_message": next_persist_message,
            "next_display_kind": next_display_kind, "next_reply_expected": next_reply_expected,
        }

    async def _run_agent_queued_followup(
        self, turn_ctx: TurnContext, adapter: Any, pending: Optional[str], pending_event: Any,
        response: Any, result: Any, stream_task: Any,
    ) -> Any:
        """Run the queued / interrupting follow-up as the next turn (recursive ``_run_agent``)."""
        from gateway.run import _preserve_queued_followup_history_offset
        source, session_id, session_key, run_generation = (
            turn_ctx.source, turn_ctx.session_id, turn_ctx.session_key, turn_ctx.run_generation,
        )
        _interrupt_depth, history, _status_thread_metadata = (
            turn_ctx._interrupt_depth, turn_ctx.history, turn_ctx._status_thread_metadata,
        )
        logger.debug("Processing pending message: '%s...'", (pending or "")[:40])

        # Clear the interrupt event so the recursive _run_agent isn't re-interrupted (infinite loop).
        _active = getattr(adapter, "_active_sessions", None) if adapter else None
        if _active and session_key and session_key in _active:
            _active[session_key].clear()

        # Cap recursion depth (user keeps sending while the agent keeps failing).
        # (#816)
        if _interrupt_depth >= self._MAX_INTERRUPT_DEPTH:
            return self._queued_followup_at_depth_cap(
                turn_ctx, adapter, pending, pending_event, source, session_key, response, history,
            )

        if not result.get("interrupted"):
            deferred_result = await self._deliver_queued_followup_response(
                turn_ctx, adapter, pending, pending_event, response, result, stream_task,
                source, session_key, history,
            )
            if deferred_result is not None:
                return deferred_result

        # This method is owned by the execution mixin, not by every runner double that calls the
        # facade unbound in tests and compatibility integrations.
        prepared = await GatewayTurnExecutionMixin._prepare_queued_followup_inputs(
            self, turn_ctx, pending, pending_event, response, result, source,
            cast(str, session_id), cast(str, session_key), history,
        )
        if prepared is None:
            return result
        updated_history = prepared["updated_history"]
        next_source = prepared["next_source"]
        next_message = prepared["next_message"]
        next_session_key = prepared["next_session_key"]
        next_message_id = prepared["next_message_id"]
        next_inbound_id = prepared["next_inbound_id"]
        next_channel_prompt = prepared["next_channel_prompt"]
        next_message_type = prepared["next_message_type"]
        next_persist_message = prepared["next_persist_message"]
        next_display_kind = prepared["next_display_kind"]
        next_reply_expected = prepared["next_reply_expected"]

        # Clear the prior turn's streaming-TTS completion marker so the recursive turn isn't suppressed (#60671).
        _clear_adapter = self._delivery_adapter_for(source)
        _completed_turns = getattr(_clear_adapter, "_streaming_tts_completed_turns", None)
        _prior_key = getattr(_clear_adapter, "_streaming_tts_turn_key", None)
        if _completed_turns is not None and callable(_prior_key) and session_key and run_generation is not None:
            _pk = _prior_key(session_key, run_generation)
            if _pk:
                _completed_turns.discard(_pk)

        # Restart the typing indicator; the outer typing task may be stale.
        if _clear_adapter:
            with suppress(Exception):
                await _clear_adapter.send_typing(source.chat_id, metadata=typing_metadata_for_turn(
                    _clear_adapter, source.chat_id, next_message_id, _status_thread_metadata))

        # Re-baseline the cached agent's message_count snapshot before recursing into the in-band queued
        # (/queue) follow-up turn. The first turn has completed and flushed its own user + assistant rows to
        # the SessionDB, so the cross-process coherence guard (#45966) — which this recursive _run_agent
        # call re-enters — would otherwise see the grown on-disk count against the stale build-time snapshot
        # and rebuild the agent on THIS process's OWN writes, destroying the prompt-cache prefix #46237 was
        # merged to preserve. The existing re-baseline in _handle_message_with_agent only runs after the
        # whole _run_agent chain unwinds — too late for the in-band follow-up. Use the same (session_key,
        # session_id) the recursive call runs under so the snapshot matches exactly what the follow-up's
        # guard will consult. Fail-safe in helper.
        # Acknowledge the follow-up the way an idle-session message is: this in-band drain is the only
        # place a queued/interrupting message ever runs, so base.py's hook site is never entered for it.
        # Resolve the adapter from the follow-up's OWN source — a multiplexed gateway can route it to a
        # different profile's adapter, and only that instance holds the per-message reaction state.
        from gateway.run_turn_followup_ack import _followup_cancel_outcome, _run_followup_processing_hook
        _hook_adapter = self._intake_adapter_for(next_source) if pending_event is not None else None
        await _run_followup_processing_hook(_hook_adapter, pending_event, "on_processing_start")
        # The re-baseline sits inside the try: a /stop landing on its DB await must still close the marker
        # (the helper's own ``except Exception`` does not catch cancellation).
        try:
            await self._refresh_agent_cache_message_count(session_key, session_id)
            if pending_event is not None and getattr(self, "_is_gateway_wake")(pending_event):
                next_message = await getattr(self, "_internal_wake_input_at_execution")(
                    pending_event, session_id, next_message)
                if next_message is None:
                    logger.info("Discarding queued wake superseded during preparation for %s", session_key or "?")
                    await _run_followup_processing_hook(
                        _hook_adapter, pending_event, "on_processing_complete", ProcessingOutcome.SUCCESS)
                    return result

            # Each drained follow-up is its own turn: bind its origin, not the chain head's.
            # Event-less interrupt/steer text continues the turn it follows, so it keeps that origin.
            _head_origin = bound_turn_origin()
            if pending_event is not None:
                set_turn_origin(turn_origin_for_event(pending_event))
                # In-band turns bypass the inbound slot claim. Keep the conversation clock on
                # the latest actual input, like its terminal goal identity; never restore the head.
                self._session_state(next_session_key).conversation.last_turn_started_at = time.time()
            _consumed_events = (
                (pending_event, *getattr(pending_event, "_hermes_absorbed_events", ()))
                if pending_event is not None else ()
            )
            # In-band turns bypass _handle_message_with_agent. Record their own
            # result, including absorbed inputs, rather than borrowing the chain head's.
            for consumed_event in _consumed_events:
                consumed_event._agent_turn_succeeded = False
            from gateway.reply_latency import bind_agent_start, current_receipt, restore_receipt
            _head_receipt = current_receipt()
            try:
                bind_agent_start(pending_event)
                _followup_receipt = current_receipt()
                actor_scope = (scoped_turn_actor(pending_event, self._session_state(next_session_key).turn)
                               if pending_event is not None else nullcontext())
                with actor_scope:
                    loop_completion_identity = await self._run_in_executor_with_context(
                        loop_completion.capture_loop_identity, session_id)
                    followup_result = await self._run_agent(
                        message=next_message, context_prompt=turn_ctx.context_prompt, history=updated_history,
                        source=next_source, session_id=session_id, session_key=next_session_key,
                        run_generation=run_generation, _interrupt_depth=_interrupt_depth + 1,
                        event_message_id=next_message_id, inbound_message_id=next_inbound_id,
                        recovery_id=getattr(pending_event, "_hermes_recovery_id", None),
                        channel_prompt=next_channel_prompt, message_type=next_message_type,
                        persist_user_message=next_persist_message,
                        _post_delivery_adapter=getattr(turn_ctx, "_post_delivery_adapter", None) or adapter,
                        persist_user_display_kind=next_display_kind,
                        reply_expected=next_reply_expected,
                        persist_user_display_metadata={
                            **reply_expected_metadata(next_reply_expected), **diagnostic_metadata(pending_event)} or None,
                    )
                from gateway.run import _should_clear_resume_pending_after_turn
                for consumed_event in _consumed_events:
                    consumed_event._agent_turn_succeeded = _should_clear_resume_pending_after_turn(followup_result)
            finally:
                restore_receipt(_head_receipt)
                set_turn_origin(_head_origin)
        except asyncio.CancelledError:
            await _run_followup_processing_hook(
                _hook_adapter, pending_event, "on_processing_complete", _followup_cancel_outcome(_hook_adapter))
            raise
        except BaseException:
            await _run_followup_processing_hook(
                _hook_adapter, pending_event, "on_processing_complete", ProcessingOutcome.FAILURE)
            raise
        await _run_followup_processing_hook(
            _hook_adapter, pending_event, "on_processing_complete", ProcessingOutcome.SUCCESS)
        merged = _preserve_queued_followup_history_offset(result, followup_result)
        merged = loop_completion.carry_followup(
            merged, loop_completion_identity, self._post_turn_goal_identity(pending_event).get("loop_tick"))
        # The TERMINAL turn of the chain owns the ledger identity for the outer final send, which
        # the adapter brackets against the event that OPENED the chain. Without this the terminal
        # reply is recorded under the first message's id, so a first reply that was refused (flood
        # control) has its outstanding row replaced and marked delivered by an identical-text
        # terminal reply, and is never redelivered. A deeper recursion has already set its own id,
        # so only fill the key while it is still absent: the innermost turn wins.
        if isinstance(merged, dict) and "queued_terminal_inbound_id" not in merged:
            merged = {
                **merged,
                "queued_terminal_inbound_id": next_inbound_id,
                "queued_terminal_reply_receipt": _followup_receipt,
                "queued_terminal_display_kind": next_display_kind,
                "queued_terminal_reply_expected": next_reply_expected,
                "queued_terminal_source": next_source,
                "queued_terminal_notification_category": (
                    (pending_event.metadata or {}).get("notification_category", "result")
                    if pending_event is not None and pending_event.internal else "result"),
                "queued_terminal_notification_origin": (
                    (pending_event.metadata or {}).get("notification_origin")
                    if pending_event is not None and pending_event.internal else None),
                # The chain's single post-turn judge evaluates the TERMINAL turn's response, so it
                # must see that turn's goal-continuation identity and origin, not the head's.
                "queued_terminal_goal_identity": self._post_turn_goal_identity(pending_event),
            }
        return merged

    async def _run_agent_cleanup_turn_tasks(
        self, turn_ctx: TurnContext, *, progress_task: Any, log_task: Any, interrupt_monitor: asyncio.Task,
        _notify_task: asyncio.Task, tracking_task: asyncio.Task, stream_task: Any,
    ) -> None:
        """``finally`` half of a turn: cancel background tasks, flush stream, release the session slot."""
        stream_consumer_holder, session_key = turn_ctx.stream_consumer_holder, turn_ctx.session_key
        for task in (progress_task, log_task, interrupt_monitor, _notify_task):
            if task:
                task.cancel()

        if stream_task:
            # No stream consumer was created: nothing to flush, cancel instead of waiting out 5s.
            if not (stream_consumer_holder and stream_consumer_holder[0] is not None):
                stream_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stream_task
            else:
                await self._await_stream_task(stream_task)

        # Abort + bounded wait for streaming TTS: covers paths where normal finalisation was skipped.
        _stts_finally = turn_ctx.streaming_tts_consumer_holder[0]
        # See #60671.
        if _stts_finally is not None and not _stts_finally.done:
            _stts_finally.abort("cleanup")
            with suppress(Exception):
                await _stts_finally.wait_complete(timeout=2.0)

        tracking_task.cancel()
        if session_key:
            # Release the slot only if this run's generation still owns it (/stop or /new may have
            # installed its own state).
            self._release_running_agent_state(session_key, run_generation=turn_ctx.run_generation)
        if self._draining:
            self._update_runtime_status("draining")

        for task in (progress_task, log_task, interrupt_monitor, tracking_task, _notify_task):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    # A background task that died of a real error must not abort the cleanup path.
                    logger.debug("background turn task failed during cleanup", exc_info=True)

    async def _run_agent_edit_streamed_message(
        self, _sc, source, response, content, *, _sk, ok, fail_result: str, fail_exc: str,
    ) -> None:
        """Edit the stream consumer's message in place with ``content``; on success mark
        ``response["already_sent"]`` and log ``ok``. A returned failure logs ``fail_result`` as
        ``(session, error)`` and an exception logs ``fail_exc`` as ``(session, exc)``; either way
        ``already_sent`` stays unset so the normal final send delivers the content."""
        # Keep the adapter-specific inline copy display on reconcile edits too.
        from gateway.copy_blocks import copy_free_text_for
        content = copy_free_text_for(_sc.adapter, content or "")
        try:
            _res = await _sc.adapter.edit_message(
                chat_id=source.chat_id, message_id=_sc.message_id, content=content, finalize=True,
            )
        except Exception as _edit_err:
            logger.warning(fail_exc, _sk, _edit_err)
            return
        if not getattr(_res, "success", True):
            logger.warning(fail_result, _sk, getattr(_res, "error", None))
            return
        response["already_sent"] = True
        logger.info(*ok)

    async def _run_agent_mark_streamed_delivery(self, response: Any, turn_ctx: TurnContext) -> None:
        """Set ``response["already_sent"]`` when streaming already delivered the final reply.

        Never when the agent failed (unseen error), on "(empty)", or on a finite (`-z`, `chat -q`) turn:
        its viewer prints only the completion text, never the deltas (#139666). Both flags reflect call
        success, not content: a mismatch with the recorded turn-final payload never suppresses."""
        from gateway.session_finite import finite_turn_required
        _sc, source, session_key = turn_ctx.stream_consumer_holder[0], turn_ctx.source, turn_ctx.session_key
        if not isinstance(response, dict) or response.get("failed") or finite_turn_required() is True:
            return
        _final = response.get("final_response") or ""
        _is_empty_sentinel = not _final or _final == "(empty)"
        # response_previewed: only suppress if that EXACT text was delivered, not unrelated commentary.
        # Unrelated commentary/progress must not be mistaken for the final response (#14238).
        _previewed = bool(response.get("response_previewed"))
        _content_delivered = bool(_sc and getattr(_sc, "final_content_delivered", False))
        # #71643: a *successful* finalize edit can still carry only the last preview snapshot — deltas
        # generated between that edit and stream completion never reach any API call, and both suppression
        # flags are set from the call's success rather than its content. Reconcile the consumer's recorded
        # turn-final payload against the completed response: on a demonstrable mismatch (False) neither
        # final_response_sent nor final_content_delivered may suppress the normal final send. False also
        # covers payload-less multi-message split delivery (#78541). None (no record on a non-split legacy
        # path) keeps legacy trust; the failed-finalize family (#51828 / #33793) is unaffected because those
        # paths leave the flags False or record the complete fallback payload.
        _stale_finalized = False
        if _content_delivered and not _is_empty_sentinel:
            _matcher = getattr(_sc, "delivered_final_matches", None)
            if callable(_matcher):
                with suppress(Exception):
                    _stale_finalized = _matcher(_final) is False
            if _stale_finalized:
                _content_delivered = False
        # Plugin hooks may append content after streaming finished — then send the final version.
        _transformed = bool(response.get("response_transformed"))
        # Suppress the normal send only when the actual final reply reached the user.
        _streamed = self._run_agent_stream_confirmed_final_delivery(_sc, _final, previewed=_previewed)
        if _is_empty_sentinel:
            return
        _sk = session_key or "?"
        if not _transformed and (_streamed or _content_delivered):
            logger.info("Suppressing normal final send for session %s: final delivery already confirmed (streamed=%s previewed=%s content_delivered=%s).", _sk, _streamed, _previewed, _content_delivered)
            # A stream send counted as shown on a timeout may be lost: the ledger resends it marked.
            response["already_sent"], response["delivery_uncertain"] = True, getattr(_sc, "delivery_uncertain", False) is True
        elif _sc is not None and getattr(_sc, "_abandoned_draft_message_id", None):
            if not turn_ctx._run_still_current() or response.get("interrupted"):
                return  # Do not fall through to plugin-transformed reconciliation either.
            # A dead consumer sealed its owned preview: finish that message, never infer
            # ownership from thread metadata or post a duplicate beside the orphan.
            await self._run_agent_edit_streamed_message(
                _sc, source, response, _final, _sk=_sk,
                ok=("Reconciled abandoned draft for session %s in message %s.", _sk, _sc.message_id),
                fail_result="Abandoned-draft edit failed for session %s (%s); sending final normally.",
                fail_exc="Abandoned-draft edit failed for session %s: %s; sending final normally.",
            )
        elif not _transformed and _stale_finalized and _sc is not None:
            # Stale finalize: edit the streamed message up to the complete response (on failure the
            # normal send delivers). Not for split delivery — message_id is only the LAST chunk.
            _sc_msg_id = _sc.message_id
            if getattr(_sc, "_turn_split_delivery", False):
                logger.info(
                    "Stale streamed finalize detected for session %s on a multi-message split; skipping the in-place reconciliation edit and delivering the complete response via normal final send (#78541).",
                    _sk,
                )
            elif _sc_msg_id and _sc_msg_id != "__no_edit__" and getattr(_sc, "adapter", None) is not None:
                await edit_streamed_message(
                    _sc, source, response, _final, sk=_sk,
                    ok=("Reconciled stale streamed finalize for session %s: edited message %s with the complete response (#71643).", _sk, _sc_msg_id),
                    fail_result="Stale-finalize reconciliation edit failed for session %s (%s); sending complete response via normal final send.",
                    fail_exc="Stale-finalize reconciliation edit failed for session %s: %s; sending complete response via normal final send.",
                )
            else:
                logger.info(
                    "Stale streamed finalize detected for session %s with no editable message; delivering complete response via normal final send (#71643).",
                    _sk,
                )
        elif _transformed and _sc is not None:
            # Transformed after streaming: edit the streamed message instead of sending a duplicate.
            if _sc.message_id:
                await edit_streamed_message(
                    _sc, source, response, response["final_response"], sk=_sk,
                    ok=("Edited streamed message %s for session %s to include plugin-transformed content.", _sc.message_id, _sk),
                    fail_result="Transformed-final edit failed for session %s (%s); sending transformed response via normal final send.",
                    fail_exc="Failed to edit streamed message for session %s: %s",
                )
        elif not _transformed and owns_reconcilable_preview(_sc, getattr(source, "chat_id", ""), _final):
            # Unconfirmed final, one real preview on screen (a flood-frozen bubble, a cancelled
            # consumer): finish it in place; a plain final send would post the answer twice.
            await edit_streamed_message(
                _sc, source, response, _sc._clean_for_display(_final), sk=_sk,
                ok=("Finished streamed preview %s in place for session %s (final not confirmed).", _sc.message_id, _sk),
                fail_result="Unconfirmed-final preview edit failed for session %s (%s); sending normally.",
                fail_exc="Unconfirmed-final preview edit failed for session %s: %s; sending normally.")
        elif _sc is not None and getattr(_sc, "stream_deltas_enabled", True):
            # DUPLICATE-RISK DIAGNOSTIC: a stream consumer existed but suppression did NOT fire; log
            # the decision inputs ("signal never set" vs "ack-pending race"). Skipped for consumers
            # never fed the final's deltas (interim-only wiring, #105341) — they cannot have raced
            # the normal final send, so the warning would be a guaranteed false positive.
            logger.warning(
                "Normal final-send NOT suppressed despite active stream consumer for session %s: "
                "streamed=%s previewed=%s content_delivered=%s transformed=%s final_len=%d — "
                "possible duplicate send (see wecom ack-timeout RCA).",
                _sk, _streamed, _previewed, _content_delivered, _transformed, len(_final),
            )
        if isinstance(response, dict) and response.get("already_sent") and _sc is not None:
            delivered_id = getattr(_sc, "message_id", None)
            if delivered_id and str(delivered_id) != "__no_edit__":
                # Last chunk of a split stream: the surviving message a reaction targets.
                response["_delivered_platform_message_id"] = str(delivered_id)

    def _progress_card_recorder(self, turn_ctx: TurnContext) -> Callable[[str], None]:
        """Durably record each temporary card as it is sent: the deletion callback below lives only
        in memory, and a restart before it fires would strand the card (boot deletes orphans).
        Tracking sites run on the loop or a worker thread, so the write is handed to the loop and
        then off it."""
        loop, pending, forgotten = asyncio.get_running_loop(), turn_ctx._card_writes, turn_ctx._card_forgotten
        key, chat_id = turn_ctx.session_key, turn_ctx.source.chat_id

        def spawn(message_id: str) -> None:
            if message_id in forgotten:  # its forget ran while this record was queued: it must not outlive it
                return
            task = self._retain_background_task(asyncio.ensure_future(
                self._persist_progress_cards("record_progress_card", key, chat_id, message_id)))
            pending.add(task)
            task.add_done_callback(pending.discard)

        def record(message_id: str) -> None:
            with suppress(RuntimeError):  # loop already closed: the card just goes unrecorded
                loop.call_soon_threadsafe(spawn, message_id)
        return record

    async def _persist_progress_cards(self, method: str, session_key: str, *args: Any) -> None:
        try:
            await getattr(self.async_session_store, method)(session_key, *args)
        except Exception as exc:
            logger.warning("Progress card %s failed for session %s: %s", method, session_key, exc, exc_info=True)

    async def _forget_turn_progress_cards(self, turn_ctx: TurnContext, message_ids: list[str]) -> None:
        # A record still in flight would otherwise land after its forget and outlive it: wait for the
        # spawned ones, and mark the ids first so one still queued for the loop is dropped when it runs.
        turn_ctx._card_forgotten.update(message_ids)
        pending = list(turn_ctx._card_writes)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self._persist_progress_cards(
            "forget_progress_cards", turn_ctx.session_key, turn_ctx.source.chat_id, message_ids)

    def _run_agent_schedule_bubble_cleanup(self, response: Any, _cleanup_adapter: Any, turn_ctx: TurnContext) -> None:
        """Register turn-local deletion, awaited after confirmed final delivery."""
        ids, key = turn_ctx._cleanup_msg_ids, turn_ctx.session_key
        delivery = getattr(turn_ctx, "_status_delivery", None)
        if delivery is not None:
            delivery.closed = True
        if getattr(turn_ctx, "_bubble_cleanup_registered", False):
            return
        if not (
            turn_ctx._cleanup_progress and _cleanup_adapter is not None and (ids or delivery is not None) and key
            and isinstance(response, dict) and not response.get("failed")
            and hasattr(_cleanup_adapter, "register_post_delivery_callback")
        ):
            if turn_ctx._cleanup_progress and ids and isinstance(response, dict) and response.get("failed"):
                logger.info("Temp bubble cleanup skipped for session %s: failed run", key)
                if key:  # failed runs keep their cards on purpose, so boot must not delete them either
                    self._retain_background_task(asyncio.ensure_future(
                        self._forget_turn_progress_cards(turn_ctx, list(ids))))
            return
        owner = turn_ctx._post_delivery_adapter or _cleanup_adapter
        chat_id = turn_ctx.source.chat_id

        async def cleanup() -> None:
            if delivery is not None:
                delivery.cleaned = True
            snapshot = list(dict.fromkeys(ids))
            deleted = 0
            failures = []
            # Group by owning adapter so a platform with a batch API spends one request per turn,
            # not one per bubble, out of the chat's shared outbound budget.
            by_owner: dict = {}
            for mid in snapshot:
                live = (delivery.owners.get(mid) if delivery is not None else None) or self._delivery_adapter_for(turn_ctx.source) or _cleanup_adapter
                by_owner.setdefault(id(live), (live, []))[1].append(mid)
            for live, owned in by_owner.values():
                try:
                    batch_delete = getattr(live, "delete_messages", None)
                    if len(owned) > 1 and inspect.iscoroutinefunction(batch_delete):
                        outcome = await batch_delete(chat_id, owned)
                    else:
                        outcome = {mid: await live.delete_message(chat_id, mid) for mid in owned}
                    for mid in owned:
                        if outcome.get(str(mid)) or outcome.get(mid):
                            deleted += 1
                        else:
                            failures.append(f"{mid}:returned_false")
                except asyncio.CancelledError:
                    logger.warning("Temp bubble cleanup cancelled for session %s: remaining=%d", key,
                                   len(snapshot) - deleted - len(failures))
                    raise
                except Exception as error:  # health: allow BLE001 -- any delete failure is recorded per message and logged below
                    failures.extend(f"{mid}:{type(error).__name__}" for mid in owned)
            # Settled after one attempt, as before the record existed: a cancelled cleanup (shutdown)
            # re-raised above and keeps its record, so the next boot finishes it.
            await self._forget_turn_progress_cards(turn_ctx, snapshot)
            if failures:
                logger.warning("Temp bubble cleanup failures for session %s: %s", key,
                               ", ".join(failures[:10]))
            logger.info("Temp bubble cleanup complete for session %s: requested=%d deleted=%d failed=%d",
                        key, len(snapshot), deleted, len(failures))

        cleanup._temporary_cleanup = True
        owner.register_post_delivery_callback(key, cleanup, generation=turn_ctx.run_generation)
        turn_ctx._post_delivery_adapter = owner
        turn_ctx._bubble_cleanup_registered = True
        # A FIFO drain turn has no adapter handler whose unwind fires this; the drain fires it.
        from gateway.session_results import execution_result
        captured = execution_result.get()
        if captured is not None:
            captured['post_delivery'] = (owner, key, turn_ctx.run_generation)
