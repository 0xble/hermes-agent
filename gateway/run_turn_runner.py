"""Per-turn callback runner (progress/status/voice/run_sync) for the gateway agent turn.

``TurnRunner`` owns the per-turn callbacks ``GatewayRunner._run_agent_inner`` binds. ``gateway.run``
internals are imported lazily inside method bodies (import cycle), so ``patch("gateway.run.X")``
keeps intercepting them at call time.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import threading
import time
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from agent.i18n import t
from agent.interrupt_compat import _accepts_keyword
from agent.replay_cleanup import canonicalize_replay_history
from gateway.attended_model import (
    format_attended_switch_back_notice, format_attended_switch_notice, parse_attended_config,
)
from gateway.config import Platform
from agent.notice_style import notice_metadata
from gateway.media_repair import repair_explicit_computer_use_media_paths
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.typing_state import pause_typing_for_prompt, resume_typing_after_prompt, typing_metadata_for_source
from gateway.platforms.base_exec_approval import ea_default_reason_text
from gateway.run_task_card_progress import TaskCardProgressMixin
from gateway.run_turn_runner_progress import _ToolProgressLine, send_progress_text
from gateway.turn_context import TurnContext, silence_retry_prompt
from hermes_cli.config import cfg_get
from utils import is_truthy_value
from gateway.run_turn_progress import GatewayTurnProgressMixin, _viewer_status_kind, local_viewer_turn
from gateway.session_execution import GatewaySessionAgentMixin
from gateway.turn_session_titles import TurnSessionTitlesMixin

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.run import GatewayRunner

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")

# Consecutive turns a session's persisted transcript may lag its live cached history before
# _load_turn_history escalates the lag line from WARNING to ERROR (#114266: 11 days of WARNING).
_TRANSCRIPT_LAG_ESCALATION_TURNS = 3



def _resolution_failure_reply(exc: Exception, platform: Platform) -> tuple[str, str, bool]:
    """Return chat text, failure reason and retryability for pre-turn resolution failures."""
    from agent.error_classifier import FailoverReason
    from hermes_cli.auth import AuthError, is_rate_limited_auth_error
    from hermes_state_runtime import RuntimeStoreError
    auth_permanent = FailoverReason.auth_permanent.value
    if isinstance(exc, RuntimeStoreError):
        return (f"⚠️ This session's launch credentials are no longer available ({exc.reason}), so this "
                "message wasn't processed. Start a new session from the CLI to bind them again.", auth_permanent, False)
    if is_rate_limited_auth_error(exc.__cause__):
        from gateway.run import _gateway_provider_error_reply
        return _gateway_provider_error_reply(str(exc)), auth_permanent, False
    if platform == Platform.LOCAL:
        return f"⚠️ {exc}", auth_permanent, False
    seen: set[int] = set()
    host = auth = False
    link: BaseException | None = exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        host = host or isinstance(link, OSError)
        auth = auth or isinstance(link, AuthError)
        link = link.__cause__ or link.__context__
    if host and not auth:
        return t("gateway.errors.generic_failed"), "host_error", False
    return t("gateway.errors.no_credentials"), auth_permanent, False


# run_conversation result keys the gateway projection carries verbatim so a finite viewer's
# `-z --usage-file` ledger (hermes_cli/oneshot._USAGE_KEYS) reads what the in-process path did.
_LEDGER_PASSTHROUGH_KEYS = (
    "estimated_cost_usd", "cost_status", "cost_source", "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens", "total_tokens", "provider", "turn_exit_reason", "service_tier",
)


def _renders_exec_approval_buttons(adapter_cls: type) -> bool:
    """True when the adapter class renders native approval buttons. BasePlatformAdapter subclasses
    say so through ``supports_exec_approval_buttons``; anything else (test doubles, relay-style
    duck types) counts when it defines ``send_exec_approval`` itself."""
    probe = getattr(adapter_cls, "supports_exec_approval_buttons", None)
    if callable(probe) and issubclass(adapter_cls, BasePlatformAdapter):
        return bool(probe())
    return getattr(adapter_cls, "send_exec_approval", None) is not None


# Rendered on a native clarify card whose wait ended without a click (mirrors the notice the
# Slack click handler shows on a dead entry).
def _clarify_expired_notice() -> str:
    return t("gateway.clarify.expired")
# Beside ``failure_reason`` and ``error``: what ``agent.error_surface.build_error_surface_from_result`` reads,
# so the authority's ``message.complete`` carries the same error card the TUI path builds;
# ``usage_window_message`` also marks a window-plan refusal for the chat rewording.
_FAILURE_DESCRIPTOR_KEYS = ("free_tier", "failure_retryable", "failure_resets_at", "usage_window_message")


def _finite_viewer_turn() -> bool:
    from gateway.session_finite import finite_turn_required
    return finite_turn_required() is True


def _persisted_turn(agent, agent_history, result, compressions) -> dict | None:
    """``message.complete.persisted_turn`` for this turn, the receipt the TUI gateway's own turns
    publish: clients bind the streamed reply to its stored row instead of comparing words. The
    agent copies ``conversation_history`` into its message list, so an untouched pre-turn prefix
    is the same dicts (transcript rows here carry no ``_row_id`` to compare)."""
    from agent.persisted_turn_receipt import persisted_turn_receipt
    if not isinstance(result, dict):
        return None
    status = ("interrupted" if result.get("interrupted")
              else "error" if result.get("failed") or result.get("error") else "complete")
    return persisted_turn_receipt(
        result.get("messages"), getattr(agent, "_persist_user_message_idx", None), agent_history,
        result.get("final_response"), status,
        compression_unchanged=type(compressions) is int and compressions == getattr(
            getattr(agent, "context_compressor", None), "compression_count", None),
        prefix_row_matches=lambda before, after: before is after)


class _NoStreamConsumer(RuntimeError):
    """Raised by the delta callback when nothing consumed the text; ``_call_quietly`` turns it into
    'not delivered' so the agent's partial-delivery accounting matches what the user saw."""


class _ExecApprovalDeclined(RuntimeError):
    """The connector refused the approval card's destination.

    Raised (not returned) so it propagates out of `_approval_notify_sync` to
    `_await_gateway_decision`, whose notify-failure path drops the central
    approval queue entry and unblocks the waiting tool. A plain return
    suppressed the text fallback but left that entry pending.
    """


class TurnRunner(TurnSessionTitlesMixin, TaskCardProgressMixin, GatewayTurnProgressMixin, GatewaySessionAgentMixin):
    """Per-turn callbacks, with routing assignment supplied by gateway turn assembly."""

    # ``None`` is a legitimate state: turns without a canonical session authority publish no
    # execution events and take no controls snapshot.
    _approval_owner = None

    def __init__(self, runner: GatewayRunner, ctx: TurnContext, *, model_assignment: Optional[dict] = None) -> None:
        self._runner = runner
        self._ctx = ctx
        self._model_assignment = model_assignment
        from gateway.status_delivery import StatusDelivery
        self._status_delivery = StatusDelivery(ctx, lambda: runner._delivery_adapter_for(ctx.source))
        ctx._status_delivery = self._status_delivery
        from gateway.session_authorities import active_authority
        authority = active_authority(runner)
        if authority is not None:
            source = getattr(ctx, "source", None)
            owner_id = (source.chat_id if getattr(source, "platform", None) == Platform.LOCAL
                        else authority.logical_owner(getattr(ctx, "session_id", None)))
            if owner_id in authority.sessions:
                generation = authority.db.get_session(owner_id)["runtime_generation"]
                self._approval_owner = (authority, owner_id, generation)

    def _publish_execution(self, event_type, payload):
        if self._approval_owner is not None:
            authority, session_id, generation = self._approval_owner
            return authority.publish_execution(session_id, generation, event_type, payload)
        return False

    def _publish_api_tool(self, event_type, call_id, tool_name, args, result=None):
        if self._approval_owner is not None:
            from gateway.session_api_turn import publish_api_tool_event
            authority, session_id, generation = self._approval_owner
            publish_api_tool_event(authority, session_id, generation, event_type,
                                   str(call_id or ""), str(tool_name or "tool"), args, result)

    # ── shared thread→loop plumbing ─────────────────────────────────────────────────────────

    def _schedule(self, coro, log_message: str, loop=None):
        """Hop a coroutine from the agent's sync worker thread onto the gateway loop."""
        from gateway.run import safe_schedule_threadsafe
        return safe_schedule_threadsafe(
            coro, self._ctx._loop_for_step if loop is None else loop, logger=logger, log_message=log_message,
        )

    def _agent_interrupted(self) -> bool:
        """True once the user sent `stop` (agent_holder[0] is the shared agent handle)."""
        try:
            agent = self._ctx.agent_holder[0] if self._ctx.agent_holder else None
            return bool(agent is not None and getattr(agent, "is_interrupted", False))
        except Exception:
            return False

    def _stream_consumer(self):
        holder = self._ctx.stream_consumer_holder
        return holder[0] if holder else None

    def _drain_progress_queue(self) -> None:
        q = self._ctx.progress_queue
        with suppress(Exception):
            while not q.empty():
                q.get_nowait()

    def _track_progress_result(self, result) -> None:
        """Remember a delivered progress/status message id for end-of-turn cleanup."""
        ctx = self._ctx
        if ctx._cleanup_progress and getattr(result, "success", False) and getattr(result, "message_id", None):
            ctx._cleanup_msg_ids.append(str(result.message_id))

    def _track_future_cleanup_id(self, fut) -> None:
        try:
            res = fut.result()
        except Exception:
            return
        self._track_progress_result(res)

    # ── progress_callback (agent thread → progress queue) ───────────────────────────────────

    def progress_callback(self, event_type: str, tool_name: str | None = None, preview: str | None = None, args: dict | None = None, **kwargs):
        """Delegate to the canonical progress mixin, including local-viewer frames."""
        return GatewayTurnProgressMixin.progress_callback(self, event_type, tool_name, preview, args, **kwargs)

    def _progress_subagent_notice_legacy(self, preview, kwargs: dict) -> None:
        """Only terminal failure statuses render (same notice rail as credit warnings)."""
        ctx = self._ctx
        from gateway.warning_notifications import render_notification
        status = kwargs.get("status")
        try:
            from tools.delegate_tool import SUBAGENT_FAILURE_STATUSES, format_subagent_failure_line
            from tools.delegate_tool_progress import subagent_failure_notice_claimed
            claimed = kwargs.get("failure_notice_claimed")
            if claimed is None and status in SUBAGENT_FAILURE_STATUSES:
                # Only events that bypassed the delegation relay reach here without a decision.
                claimed = subagent_failure_notice_claimed(
                    child_session_id=kwargs.get("child_session_id"), child_goal=kwargs.get("goal"),
                    child_status=status, error=kwargs.get("summary") or preview,
                    failure_reason=kwargs.get("failure_reason"))
            if status in SUBAGENT_FAILURE_STATUSES and ctx._run_still_current() and not claimed:
                line = format_subagent_failure_line(
                    kwargs.get("goal"), status, error=kwargs.get("summary") or preview,
                    duration_seconds=kwargs.get("duration_seconds"), failure_reason=kwargs.get("failure_reason"),
                )
                render_notification(
                    lambda: self._schedule(self._runner._deliver_platform_notice(ctx.source, line), "subagent failure notice scheduling error"),
                    platform=ctx.source.platform, user_config=ctx.user_config)
        except Exception:
            logger.debug("subagent failure notice failed", exc_info=True)

    def _progress_onboarding_hint(self, kwargs: dict) -> None:
        """First-touch onboarding: the first time a tool exceeds _LONG_TOOL_THRESHOLD_S while
        streaming every tool (progress_mode == "all"), append a one-time /verbose hint."""
        from gateway.run import _hermes_home, _load_gateway_config
        ctx = self._ctx
        try:
            if (kwargs.get("duration") or 0) >= ctx._LONG_TOOL_THRESHOLD_S and ctx.progress_mode == "all":
                from agent.onboarding import TOOL_PROGRESS_FLAG, is_seen, mark_seen, tool_progress_hint_gateway
                cfg = _load_gateway_config()
                gate_on = is_truthy_value(cfg_get(cfg, "display", "tool_progress_command"), default=False)
                if gate_on and not is_seen(cfg, TOOL_PROGRESS_FLAG):
                    ctx.long_tool_hint_fired[0] = True
                    ctx.progress_queue.put(tool_progress_hint_gateway())
                    mark_seen(_hermes_home / "config.yaml", TOOL_PROGRESS_FLAG)
        except Exception as err:
            logger.debug("tool-progress onboarding hint failed: %s", err)

    # ── editable progress bubbles (progress-queue drain) ────────────────────────────────────

    @dataclasses.dataclass
    class _ProgressEditState:
        """Mutable editable-bubble state shared by ``send_progress_messages`` and its helpers."""
        adapter: Any
        progress_lines: list
        progress_msg_id: Any
        can_edit: bool
        _progress_len_fn: Any
        _PROGRESS_TEXT_LIMIT: int
        _edit_accepts_metadata: bool
        # A second failure before the fresh bubble edits successfully means edits are unusable.
        reanchored: bool = False
        # Buffer until the monotonic flood deadline, without editing, splitting or sending.
        defer_until: float = 0.0
        # Accepted includes durable outbox deferral without a message id. Bubble resets keep this.
        tool_progress_sent: bool = False

    # Default minimum seconds between progress edits. An adapter may raise it with
    # ``PROGRESS_EDIT_INTERVAL`` (Telegram matches its per-chat interim-edit floor,
    # plugins/platforms/telegram/chat_budget.EDIT_FLOOR_SECS): a producer faster than the
    # transport delivers no extra updates, it only parks edits inside the chat's send lock.
    _PROGRESS_EDIT_INTERVAL = 3.0

    @classmethod
    def _progress_edit_interval(cls, adapter) -> float:
        try:
            value = float(getattr(adapter, "PROGRESS_EDIT_INTERVAL", None) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        return max(cls._PROGRESS_EDIT_INTERVAL, value)

    def _progress_edit_state(self, adapter) -> TurnRunner._ProgressEditState:
        ctx = self._ctx
        len_fn = adapter.message_len_fn if isinstance(adapter, BasePlatformAdapter) else len
        try:
            raw_limit = int(getattr(adapter, "MAX_MESSAGE_LENGTH", 4000) or 4000)
        except Exception:
            raw_limit = 4000
        # Per-chat resolution (relay adapter fronting N platforms): cap and length unit follow the
        # chat's underlying platform; native adapters return their scalar/property unchanged.
        if isinstance(adapter, BasePlatformAdapter):
            with suppress(Exception):
                raw_limit = int(adapter.max_message_length_for_chat(ctx.source.chat_id) or 4000)
                len_fn = adapter.message_len_fn_for_chat(ctx.source.chat_id)
        return self._ProgressEditState(
            adapter=adapter, progress_lines=[], progress_msg_id=None,
            # "separate" = one message per tool (pre-v0.9 behavior)
            can_edit=ctx.progress_grouping != "separate",
            _progress_len_fn=len_fn,
            # Leave room for platform quirks / formatting; tiny test adapters keep a usable limit.
            _PROGRESS_TEXT_LIMIT=max(1, raw_limit - (64 if raw_limit > 128 else 0)),
            # Overflow edits pass metadata (Telegram topic/thread routing) only when edit_message takes it.
            _edit_accepts_metadata=bool(ctx._progress_metadata) and _accepts_keyword(adapter.edit_message, "metadata"),
        )

    async def _edit_progress_message(self, st, message_id: str, content: str):
        ctx = self._ctx
        kwargs = {"chat_id": ctx.source.chat_id, "message_id": message_id, "content": content}
        if getattr(st.adapter, "REQUIRES_EDIT_FINALIZE", False):
            kwargs["finalize"] = True
        if st._edit_accepts_metadata:
            kwargs["metadata"] = ctx._progress_metadata
        from gateway.platforms.base import OUTBOUND_PROGRESS, outbound_class
        with outbound_class(OUTBOUND_PROGRESS):
            return await st.adapter.edit_message(**kwargs)

    @staticmethod
    def _progress_text(lines: list) -> str:
        text = "\n".join(str(line) for line in lines)
        return _ToolProgressLine(text) if any(isinstance(line, _ToolProgressLine) for line in lines) else text

    def _split_progress_groups(self, st, lines: list) -> list[list]:
        """Partition progress lines into platform-sized editable bubbles."""
        groups: list[list] = []
        current: list = []
        for line in lines:
            candidate = current + [line]
            if current and st._progress_len_fn(self._progress_text(candidate)) > st._PROGRESS_TEXT_LIMIT:
                groups.append(current)
                candidate = [line]
            current = candidate
        return groups + ([current] if current else [])



    @staticmethod
    def _is_flood_refusal(result) -> bool:
        error = (getattr(result, "error", "") or "").lower()
        return getattr(result, "retry_after", None) is not None or any(w in error for w in ("flood", "retry after"))

    @classmethod
    def _edit_failure_is_deferrable(cls, st, result) -> bool:
        """Transient and rate-limit edit failures leave the bubble editable for a later tick.

        A flood refusal says "not now", not "never": disabling edits on it turns every later tool
        line into its own message for the rest of the turn, and sending one now spends the budget
        the platform just said is exhausted.
        """
        if cls._is_flood_refusal(result):
            wait = max(float(getattr(result, "retry_after", None) or 0.0), cls._progress_edit_interval(st.adapter))
            st.defer_until = time.monotonic() + wait
            logger.info("[%s] Progress edit flood control, deferring edits for %.1fs", st.adapter.name, wait)
            return True
        if getattr(result, "retryable", False):
            logger.debug("[%s] Transient progress edit failure, retrying next tick", st.adapter.name)
            return True
        return False

    @staticmethod
    def _progress_deferred(st) -> bool:
        return time.monotonic() < st.defer_until

    @staticmethod
    def _abandon_progress_bubble(st) -> bool:
        """After a permanent edit failure, continue in a fresh bubble (True) unless the previous
        fresh bubble also failed, which means editing itself is unusable (False, can_edit off)."""
        if st.reanchored:
            st.can_edit = False
            return False
        logger.info("[%s] Progress bubble no longer editable, starting a fresh one", st.adapter.name)
        st.reanchored = True
        st.progress_msg_id = None
        return True

    @staticmethod
    def _is_reset_marker(raw) -> bool:
        return isinstance(raw, tuple) and len(raw) >= 1 and raw[0] == "__reset__"

    def _reset_progress_bubble(self, st) -> None:
        """Content bubble landed — close the tool-progress bubble so the next tool starts fresh
        below it; else tool edits hit the ORIGINAL message above (out of order)."""
        st.progress_msg_id, st.progress_lines = None, []
        self._ctx.last_progress_msg[0], self._ctx.repeat_count[0] = None, 0



    async def _drain_progress_on_cancel(self, st) -> None:
        ctx = self._ctx
        with suppress(Exception):
            while not ctx.progress_queue.empty():
                raw = ctx.progress_queue.get_nowait()
                if self._is_reset_marker(raw):
                    # Content-bubble marker during drain: close the current progress bubble
                    # and start a fresh one for tool lines that arrived after.
                    await self._roll_progress_overflow_if_needed(st)
                    await self._flush_progress_edit(st)
                    self._reset_progress_bubble(st)
                else:
                    self._progress_absorb(st, raw)
                    await self._roll_progress_overflow_if_needed(st)
        # Final edit with all remaining tools (only if editing works)
        if st.can_edit and st.progress_lines and st.progress_msg_id:
            await self._roll_progress_overflow_if_needed(st)
        await self._flush_progress_edit(st)

    async def _progress_restore_typing(self, st) -> None:
        ctx = self._ctx
        await asyncio.sleep(0.3)
        if ctx._run_still_current():
            await st.adapter.send_typing(ctx.source.chat_id, metadata=ctx._progress_metadata)



    # ── ID-bearing lifecycle callbacks (agent thread) ───────────────────────────────────────

    def voice_ack_callback(self, call_id, tool_name, args):
        """tool_start_callback: speak a one-time ack in the voice channel."""
        ctx = self._ctx
        if ctx._voice_ack_fired[0] or ctx._voice_ack_guild[0] is None or not ctx._run_still_current():
            return
        ctx._voice_ack_fired[0] = True
        adapter = self._runner.adapters.get(Platform.DISCORD)
        if adapter is None or not hasattr(adapter, "play_ack_in_voice"):
            return
        try:
            self._schedule(
                adapter.play_ack_in_voice(ctx._voice_ack_guild[0]), "voice ack scheduling error", loop=ctx._voice_ack_loop,
            )
        except Exception as err:
            logger.debug("voice ack schedule failed: %s", err)

    # Slack-native task cards ride agent.tool_start_callback / tool_complete_callback so start and
    # completion correlate by the REAL tool-call id; name-correlated progress_callback text events
    # would duplicate cards and mispair concurrent calls.

    def _native_card_gate(self) -> bool:
        ctx = self._ctx
        return bool(ctx.progress_queue) and ctx._run_still_current() and not self._agent_interrupted()

    # ── Slack-native task cards: ID-bearing lifecycle callbacks (#29483) ── These ride
    # agent.tool_start_callback / agent.tool_complete_callback so start/completion events correlate by the
    # REAL tool-call id — the name-correlated text events in progress_callback would duplicate cards and
    # mispair concurrent calls to the same tool.
    def native_tool_start_callback(self, call_id, tool_name, args):
        """Queue an ID-correlated native progress start from the agent thread."""
        if not self._native_card_gate():
            return
        from agent.display import build_tool_preview
        name = str(tool_name or "tool")
        self._ctx.progress_queue.put({
            "type": "tool.started", "tool_call_id": str(call_id or ""), "tool_name": name,
            "preview": build_tool_preview(name, args or {}, max_len=64) or "",
        })

    def native_tool_complete_callback(self, call_id, tool_name, args, result):
        """Queue the matching native completion using the real tool-call ID."""
        if not self._native_card_gate():
            return
        from agent.display import _detect_tool_failure
        name = str(tool_name or "tool")
        is_error, _ = _detect_tool_failure(name, result)
        self._ctx.progress_queue.put({
            "type": "tool.completed", "tool_call_id": str(call_id or ""), "tool_name": name, "is_error": bool(is_error),
        })

    # ── hook / status bridges (agent thread → gateway loop) ────────────────────────────────

    def _step_callback_sync(self, iteration: int, prev_tools: list) -> None:
        ctx = self._ctx
        if not ctx._run_still_current():
            return
        # prev_tools may be list[str] or list[dict] with "name"/"result" keys. Normalise so
        # "tool_names" stays backward-compatible for user hooks that do ', '.join(tool_names).
        self._publish_execution("agent.step", {"iteration": iteration})
        if iteration > 1 and local_viewer_turn(self):
            # Each step after the first follows a finished API call: main's CLI repainted its status
            # bar from the agent's counters right then (the last call is covered at turn end).
            from gateway.session_usage import publish_usage
            publish_usage(self, ctx.agent_holder[0])
        names = [(t.get("name") or "") if isinstance(t, dict) else str(t) for t in (prev_tools or [])]
        hooks = getattr(ctx, "_hooks_ref", None)
        if hooks is None or not hasattr(hooks, "emit") or not getattr(hooks, "loaded_hooks", True):
            return
        self._schedule(
            hooks.emit("agent:step", {
                "platform": ctx.source.platform.value if ctx.source.platform else "",
                "user_id": ctx.source.user_id, "session_id": ctx.session_id,
                "iteration": iteration, "tool_names": names, "tools": prev_tools,
            }),
            "agent:step hook scheduling error",
        )

    def _event_callback_sync(self, event_type: str, context: dict) -> None:
        ctx = self._ctx
        hooks = getattr(ctx, "_hooks_ref", None)
        if hooks is None or not hasattr(hooks, "emit") or ctx._loop_for_step is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(hooks.emit(event_type, context), ctx._loop_for_step)
        except Exception as e:
            logger.debug("event_callback hook error: %s", e)

    def _send_status_text(self, text: str, metadata, log_message: str) -> None:
        ctx = self._ctx
        self._schedule(ctx._status_adapter.send(ctx._status_chat_id, text, metadata=metadata), log_message)

    def _emit_persistent_attended_notice(self, message: str) -> None:
        """Deliver an attended route card outside the temporary status-bubble rail."""
        from gateway.warning_notifications import render_notification
        ctx = self._ctx
        if ctx.mute_notification_reply or not self._status_live():
            return

        def present() -> None:
            self._schedule(self._runner._deliver_platform_notice(ctx.source, message, notice_style="context"),
                           "attended notice delivery scheduling error")

        render_notification(
            present,
            platform=ctx.source.platform,
            user_config=ctx.user_config,
            diagnostic=True,
        )

    def _status_callback_sync(self, event_type: str, message: str) -> None:
        from gateway.run import _prepare_gateway_status_message, _redact_gateway_user_facing_secrets
        from gateway.warning_notifications import is_warning_status, render_notification
        ctx = self._ctx
        if ctx.mute_notification_reply or not self._status_live():
            return
        prepared = _prepare_gateway_status_message(ctx.source.platform, event_type, message)
        if prepared is None:
            logger.debug(
                "status_callback suppressed for %s/%s: %s",
                ctx.source.platform.value if ctx.source.platform else "unknown", event_type,
                _redact_gateway_user_facing_secrets(str(message or ""))[:160],
            )
            return
        def present():
            if local_viewer_turn(self):
                self._publish_execution("status.update", {"kind": _viewer_status_kind(event_type, prepared),
                                                          "text": prepared})
                return
            self._schedule(
                self._status_delivery.send(ctx._status_adapter, ctx._status_chat_id, event_type, prepared, notice_metadata(
                    ctx._status_thread_metadata, platform=ctx.source.platform, style=getattr(message, "notice_style", None))),
                f"status_callback ({event_type}) scheduling error",
            )
        render_notification(present, platform=ctx.source.platform, user_config=ctx.user_config,
                            diagnostic=is_warning_status(event_type, message))

    # ── stream consumer / interim commentary wiring ─────────────────────────────────────────

    def _setup_stream_consumer(self, platform_key):
        from gateway.stream_recovery import preview_recorder, restore_preview
        ctx = self._ctx
        if ctx.mute_notification_reply:
            return None, None, None, False
        stream_consumer = None
        # The streaming-TTS consumer is created on the outer loop thread before run_sync launches;
        # run_sync only reads it via the holder for delta-callback wiring.
        stts = ctx.streaming_tts_consumer_holder[0]
        scfg = getattr(getattr(self._runner, 'config', None), 'streaming', None)
        if scfg is None:
            from gateway.config import StreamingConfig
            scfg = StreamingConfig()
        # display.platforms.<plat>.streaming may disable streaming per platform; None = follow global.
        plat_streaming = ctx.resolve_display_setting(ctx.user_config, platform_key, "streaming")
        want_stream_deltas = not ctx.scheduled_heartbeat and scfg.enabled_for(plat_streaming)
        want_interim_messages = bool(ctx.interim_assistant_messages_enabled) and not ctx.scheduled_heartbeat
        if want_stream_deltas or want_interim_messages:
            try:
                from gateway.stream_consumer import GatewayStreamConsumer
                adapter = self._runner._delivery_adapter_for(ctx.source)
                if adapter:
                    supports_incremental_stream = (
                        getattr(adapter, "SUPPORTS_MESSAGE_EDITING", True)
                        or bool(getattr(adapter, "SUPPORTS_NATIVE_STREAMING", False))
                    )
                    consumer_stream_deltas = want_stream_deltas and supports_incremental_stream
                    consumer_cfg, pause_typing_before_finalize = self._runner._build_stream_consumer_config(
                        ctx.source, scfg, adapter,
                        # A complete commentary message needs no edit cursor.  Keeping it on the
                        # consumer records what reached non-editable platforms, so an interim
                        # callback carrying the final answer participates in final-send dedup.
                        on_missing_cursor="fallback" if want_interim_messages else "raise",
                    )
                    stream_consumer = GatewayStreamConsumer(
                        adapter=adapter, chat_id=ctx.source.chat_id, config=consumer_cfg,
                        metadata=ctx._status_thread_metadata,
                        on_new_message=(
                            (lambda: ctx.progress_queue.put(("__reset__",))) if ctx.progress_queue is not None else None
                        ),
                        on_before_finalize=pause_typing_before_finalize,
                        initial_reply_to_id=ctx.event_message_id, run_still_current=ctx._run_still_current,
                        on_preview=preview_recorder(self._runner, ctx),
                    )
                    try:
                        restore_preview(self._runner, ctx, stream_consumer,
                                        policy=self._resume_restart_policy())
                    except Exception as err:  # recovery is best-effort; never block the turn
                        logger.warning("Streamed preview restore failed: %s", err)
                    ctx.stream_consumer_holder[0] = stream_consumer
                    # #105341: a consumer created only for interim commentary (text streaming off)
                    # is never fed the final reply's deltas — mark it so the duplicate-risk
                    # diagnostic in ``_run_agent_mark_streamed_delivery`` stays silent.
                    stream_consumer.stream_deltas_enabled = consumer_stream_deltas
            except Exception as err:
                logger.debug("Could not set up stream consumer: %s", err)
        # Deltas tee to the stream consumer (when text streaming is on) and to streaming TTS.
        delta_sinks = [
            sc for sc in (
                stream_consumer if stream_consumer and stream_consumer.stream_deltas_enabled else None,
                stts,
            ) if sc is not None
        ]
        stream_delta_cb = None
        local_viewer_route = getattr(ctx.source, "platform", None) == Platform.LOCAL
        if delta_sinks or self._approval_owner is not None:
            def stream_delta_cb(text: Optional[str]) -> None:
                if not ctx._run_still_current():
                    return
                delivered = bool(delta_sinks)
                if text is not None:
                    delivered = self._publish_execution("message.delta", {"text": text}) or delivered
                elif local_viewer_route:
                    # The segment boundary closes the local reply before tool lines.
                    self._publish_execution("message.delta", {"text": None})
                for sink in delta_sinks:
                    sink.on_delta(text)
                if text is not None and (not delivered or _finite_viewer_turn()):
                    raise _NoStreamConsumer()

        def interim_assistant_cb(text: str, *, already_streamed: bool = False) -> None:
            if not ctx._run_still_current():
                return
            unseen = False
            if local_viewer_route and not already_streamed and str(text or "").strip():
                unseen = not self._publish_execution("message.interim", {
                    "text": text, "already_streamed": False,
                })
            if stts is not None:
                # Flush accepted deltas; completed commentary is a separate speech segment.
                stts.on_delta(None)
                if not already_streamed:
                    stts.on_delta(text)
                    stts.on_delta(None)
            if unseen:
                return
            if stream_consumer is not None:
                stream_consumer.on_segment_break() if already_streamed else stream_consumer.on_commentary(text)
            elif not already_streamed and ctx._status_adapter and str(text or "").strip():
                self._send_status_text(text, ctx._status_thread_metadata, "interim_assistant_callback scheduling error")

        return stream_consumer, stream_delta_cb, interim_assistant_cb, want_interim_messages

    # ── agent resolution (cache reuse vs fresh build) ───────────────────────────────────────

    @dataclasses.dataclass
    class _CachedAgentLookup:
        agent: Any = None
        reused: bool = False
        evicted: Any = None  # agent evicted under the lock; released off-lock on a daemon thread

    def _cached_sid_is_dead(self, cache_lock, cache) -> tuple:
        """(peeked cached session_id, is_dead) — checked OUTSIDE the cache lock. "cached sid != current
        sid" normally means an intentional switch (reuse), but the routing-key self-heal yields the same
        shape with an agent bound to a DEAD session; reusing it re-binds the dead sid and loops."""
        ctx = self._ctx
        peek_sid = None
        if cache_lock and cache is not None:
            with cache_lock:
                entry = cache.get(ctx.session_key)
            if entry and len(entry) > 3:
                peek_sid = entry[3]
        dead = False
        if peek_sid is not None and ctx.session_id is not None and peek_sid != ctx.session_id:
            # The cache is keyed by session_key, so the snapshot's row lives in that key's profile
            # store; its id is no longer in the routing index once the self-heal moved the key on.
            with suppress(Exception):
                dead = self._runner.session_store._is_session_ended_in_db(peek_sid, session_key=ctx.session_key)
        return peek_sid, dead

    def _current_message_count(self):
        """Cross-process write guard input: the session's current DB message_count (or None)."""
        ctx = self._ctx
        if self._runner._session_db is None or not ctx.session_id:
            return None
        count = None
        with suppress(Exception):
            # run_sync is off-loop (executor); sync DB is fine.
            row = self._runner._session_db._db.get_session(ctx.session_id)
            if row:
                count = row.get("message_count", 0)
        return count

    def _lookup_cached_agent(self, sig, cache_lock, cache, max_iterations, peek_sid, dead, msg_count):
        ctx = self._ctx
        out = self._CachedAgentLookup()
        if not (cache_lock and cache is not None):
            return out
        with cache_lock:
            cached = cache.get(ctx.session_key)
            if not cached:
                return out
            if cached[1] != sig:
                # Overwriting the slot would orphan the old instance's memory workers.
                out.evicted = self._runner._pop_cached_agent_for_eviction(ctx.session_key)
                return out
            # cached[2] = message_count at cache time (stale when a second process appended rows);
            # cached[3] = the session_id the snapshot was taken for.
            cached_mc = cached[2] if len(cached) > 2 else None
            cached_sid = cached[3] if len(cached) > 3 else None
            # Same session_key, other conversation: the counts track DIFFERENT DB rows, so the
            # comparison is meaningless — REUSE rather than bust the prompt cache on every switch.
            sid_mismatch = cached_sid is not None and ctx.session_id is not None and cached_sid != ctx.session_id
            # Re-validate the outside-lock dead-session peek against the tuple read under THIS lock:
            # a stale "dead" verdict must never be applied to a different (possibly live) agent.
            if sid_mismatch and dead and cached_sid == peek_sid:
                logger.info(
                    "Agent cache invalidated for session %s: "
                    "cached agent's session_id %s is ended in "
                    "state.db (stale self-heal artifact, "
                    "#54878 x #54947) — discarding instead of "
                    "reusing across the routing recovery", ctx.session_key, cached_sid,
                )
            elif not sid_mismatch and cached_mc is not None and msg_count is not None and msg_count != cached_mc:
                logger.info(
                    "Agent cache invalidated for session %s: "
                    "message_count changed (%s -> %s), "
                    "possible cross-process write", ctx.session_key, cached_mc, msg_count,
                )
            else:
                out.agent = cached[0]
                self._ctx.bind_agent_lifetime(out.agent)
                # Refresh LRU order so cap enforcement evicts truly-oldest entries.
                if hasattr(cache, "move_to_end"):
                    with suppress(KeyError):
                        cache.move_to_end(ctx.session_key)
                self._runner._init_cached_agent_for_turn(out.agent, ctx._interrupt_depth)
                # Cached agent may have been created with old config.
                out.agent.max_iterations = max_iterations
                logger.debug("Reusing cached agent for session %s", ctx.session_key)
                out.reused = True
                return out
            out.evicted = self._runner._pop_cached_agent_for_eviction(ctx.session_key)
        return out

    def _release_evicted_agent(self, agent) -> None:
        """Off-lock soft release on a daemon thread so teardown never blocks the gateway loop. The same
        session continues on the fresh agent, so no session-end commit (base behavior)."""
        self._runner._spawn_release_thread(
            self._runner._release_evicted_agent_soft, (agent,),
            f"agent-xproc-evict-{str(self._ctx.session_key)[:24]}",
            inline_fallback=True, session_key=self._ctx.session_key,
        )

    def _build_fresh_agent(self, turn_route, platform_key, combined_ephemeral, max_iterations,
                           reasoning_config, pr, skip_context_files):
        agent = super()._build_fresh_agent(
            turn_route, platform_key, combined_ephemeral, max_iterations,
            reasoning_config, pr, skip_context_files,
        )
        base_overrides = turn_route.get("base_request_overrides")
        if base_overrides is not None:
            agent._gateway_base_request_overrides = dict(base_overrides)
        return agent

    def _resolve_turn_agent(self, turn_route, platform_key, combined_ephemeral, max_iterations, reasoning_config, pr):
        """Reuse this session's cached AIAgent (frozen system prompt + tool schemas → prompt cache
        hits) or build a fresh one. Returns (agent, reused_cached_agent)."""
        ctx = self._ctx
        runner = self._runner
        skip_context_files = self._skip_context_files(platform_key)
        sig = runner._agent_config_signature(
            turn_route["model"], turn_route["runtime"], ctx.enabled_toolsets, combined_ephemeral,
            cache_keys=runner._extract_cache_busting_config(ctx.user_config),
            user_id=getattr(ctx.source, "user_id", None),
            user_id_alt=getattr(ctx.source, "user_id_alt", None),
            skip_context_files=skip_context_files,
        )
        cache_lock = getattr(runner, "_agent_cache_lock", None)
        cache = getattr(runner, "_agent_cache", None)
        peek_sid, dead = self._cached_sid_is_dead(cache_lock, cache)
        msg_count = self._current_message_count()
        found = self._lookup_cached_agent(sig, cache_lock, cache, max_iterations, peek_sid, dead, msg_count)
        agent = found.agent
        # Lock released — refresh the reused agent's fallback chain from disk OUTSIDE the cache lock
        # (disk I/O under the lock stalls the idle-sweep watcher and Discord heartbeats). A chain
        # configured after caching must reach the next turn; per-session serialization keeps it safe.
        if found.reused and agent is not None:
            self._runner._apply_fallback_chain_to_agent(agent, runner._refresh_fallback_model())
        if found.evicted is not None:
            self._release_evicted_agent(found.evicted)
        if agent is None:
            agent = self._build_fresh_agent(
                turn_route, platform_key, combined_ephemeral, max_iterations, reasoning_config, pr, skip_context_files,
            )
            self._ctx.bind_agent_lifetime(agent)
            if cache_lock and cache is not None:
                with cache_lock:
                    # Record the snapshot's session_id with message_count so the cross-process guard
                    # can skip the meaningless count comparison if the active session_id switches.
                    cache[ctx.session_key] = (agent, sig, msg_count, ctx.session_id)
                    runner._enforce_agent_cache_cap()
            logger.debug("Created new agent for session %s (sig=%s)", ctx.session_key, sig)
        return agent, found.reused

    # ── per-turn agent wiring ───────────────────────────────────────────────────────────────

    def _notice_callback_sync(self, notice) -> None:
        """Credits / out-of-band notices (usage bands, depletion, restored) fire from the agent's
        sync worker thread; hop onto the gateway loop. Fired-once latch lives on the cached agent."""
        from gateway.run import render_notice_line
        from gateway.warning_notifications import is_diagnostic_notice, render_notification
        if self._ctx.mute_notification_reply or not self._status_live():
            return
        # A usage-window limit line repeats the turn's own reply, which carries the sentence and the
        # switch button (a chat message cannot be cleared, so it would only pile up). The 90% heads-up
        # is a desktop / terminal nudge; in a chat it is noise (/usage has the numbers).
        key = str(getattr(notice, "key", "") or "")
        if key == "credits.window_limit" or key.startswith("credits.window."):
            return
        diagnostic = is_diagnostic_notice(notice)
        def present():
            try:
                line = render_notice_line(notice)
                if key.startswith("credits."):
                    # Window plans (e.g. a paused plan's line): a chat's /topup has no credits settings.
                    from agent.plan_limits import portal_wording
                    line = portal_wording(line)
            except Exception:
                logger.debug("render_notice_line failed", exc_info=True)
                return
            if line:
                self._schedule(self._runner._deliver_platform_notice(self._ctx.source, line, notice_style=getattr(notice, "notice_style", None)), "notice_callback delivery scheduling error")
        render_notification(present, platform=self._ctx.source.platform,
                            user_config=self._ctx.user_config, diagnostic=diagnostic)

    def _make_bg_review_callbacks(self):
        """(send, release): background-review messages ("💾 Memory updated") are held until the
        adapter's post-delivery hook releases them after the main response lands."""
        from gateway.run import _interim_metadata, _non_conversational_metadata
        ctx = self._ctx
        release_evt = threading.Event()
        pending: list[str] = []
        pending_lock = threading.Lock()

        owner = self._approval_owner
        local_viewer = owner is not None and getattr(ctx.source, "platform", None) == Platform.LOCAL

        def deliver(message: str) -> None:
            if local_viewer and owner is not None:
                authority, session_id, _generation = owner
                live = authority.sessions.get(session_id)
                if live is not None:
                    live.event_stream.publish(session_id, {"text": str(message)}, event_type="review.summary")
                return
            if self._status_live():
                self._send_status_text(
                    message,
                    notice_metadata(_interim_metadata(_non_conversational_metadata(ctx._status_thread_metadata, platform=ctx.source.platform)), platform=ctx.source.platform, style="context"),
                    "background_review_callback scheduling error",
                )

        def release() -> None:
            release_evt.set()
            with pending_lock:
                queued = list(pending)
                pending.clear()
            for message in queued:
                deliver(message)

        def send(message: str) -> None:
            if local_viewer and owner is not None:
                authority, session_id, generation = owner
                live = authority.sessions.get(session_id)
                deadline = time.monotonic() + 120
                while (live is not None and time.monotonic() < deadline
                       and live.event_stream.execution.get("execution_generation") == generation):
                    time.sleep(0.05)
                deliver(message)
                return
            if not self._status_live():
                return
            if not release_evt.is_set():
                with pending_lock:
                    if not release_evt.is_set():
                        pending.append(message)
                        return
            deliver(message)

        return send, release

    @staticmethod
    def _merge_turn_request_overrides(agent, turn_route) -> None:
        """Merge, never overwrite: init-time request overrides (e.g. a custom provider's extra_body)
        must survive every reused-agent turn. Drop only the PREVIOUS turn's routing overrides before
        layering this turn's, so stale per-turn values never linger."""
        overrides = dict(getattr(agent, "request_overrides", {}) or {})
        for key, value in (getattr(agent, "_gateway_turn_request_overrides", {}) or {}).items():
            if overrides.get(key) == value:
                overrides.pop(key, None)
        turn_overrides = dict(turn_route.get("request_overrides") or {})
        overrides.update(turn_overrides)
        agent.request_overrides = overrides
        agent._gateway_turn_request_overrides = turn_overrides

    def _apply_attended_before_api_call(self, agent, loop_state=None, *, commit=True):
        """Apply a changed desired route at the real request boundary, never on fallback retries."""
        runner = self._runner
        ctx = self._ctx
        from gateway.session_policy import policy_for_source
        # Launch-policy sessions resolve their own frozen or followed route and effort.
        # Messaging attended routing must not replace that owner's explicit choices.
        if policy_for_source(runner, ctx.source) is not None:
            return
        if getattr(agent, "is_subagent", False) or getattr(agent, "_is_auxiliary", False):
            return
        if ctx.session_key in (getattr(runner, "_bulk_reset_pending_sessions", set()) or set()):
            return
        def _handoff(*, conversation=None, runner_state=None):
            handoff = getattr(runner, "_handoff_attended_state", None)
            if callable(handoff):
                handoff(ctx.session_key, conversation=conversation, runner=runner_state)
            elif ctx.session_key and conversation:
                state = runner._session_state(ctx.session_key)
                for name, value in conversation.items():
                    setattr(state.conversation, name, value)
            if runner_state:
                for name, value in runner_state.items():
                    setattr(runner, name, value)

        try:
            from gateway.run import _load_gateway_config
            profile_home = (runner._resolve_profile_home_for_source(ctx.source)
                            if getattr(getattr(runner, "config", None), "multiplex_profiles", False) else None)
            boundary_config = _load_gateway_config(profile_home / "config.yaml") if profile_home else _load_gateway_config()
            assignment = getattr(self, "_model_assignment", None)
            decision = runner._attended_resolution_for_session(
                source=ctx.source, session_key=ctx.session_key, user_config=boundary_config,
                override=(runner._session_model_override(ctx.session_key) if ctx.session_key else None),
                allow_switch_back=commit, assignment=assignment,
            )
            if decision is None:
                return
            previous_decision = getattr(agent, "_gateway_attended_decision", None)
            previous_key = getattr(agent, "_gateway_attended_decision_key", None)
            # The pre-preflight callback resolves and applies active/explicit route changes so
            # request assembly and the first preflight gate see the destination model. The resolver
            # suppresses idle switch-back when ``allow_switch_back`` is false; only the post-preflight
            # callback may commit that boundary-only transition.
            if commit and ctx.session_key:
                _handoff(conversation={"last_api_call_at": time.time()})
            agent._gateway_attended_decision = decision
            desired_key = decision.decision_key

            def _persist_committed_route_marker():
                if not commit or not ctx.session_key:
                    return
                store = getattr(runner, "session_store", None)
                mark_route = getattr(store, "mark_attended_route_active", None)
                if callable(mark_route):
                    mark_route(ctx.session_key, decision.reason == "attended")

            if getattr(agent, "_fallback_activated", False) or getattr(agent, "_provider_fallback_active", False):
                agent._gateway_attended_decision_key = desired_key
                return
            if desired_key == previous_key:
                _persist_committed_route_marker()
                return

            if decision.switched_back:
                snapshot = getattr(runner, "_snapshot_session_runtime_state", None)
                if callable(snapshot):
                    _override, _last_model, _ = snapshot(ctx.session_key)
                    _override = (assignment or {}).get("override", _override)
                    model, runtime, _notice, _cache_model = runner._load_session_agent_runtime(
                        source=ctx.source, skey=ctx.session_key, user_config=boundary_config,
                        config=getattr(runner, "config", None), override=_override,
                        last_model=_last_model, attended_route=None,
                    )
                else:
                    model, runtime = runner._resolve_session_agent_runtime(
                        source=ctx.source, session_key=ctx.session_key, user_config=boundary_config,
                        assignment=assignment,
                    )
            else:
                model, runtime = runner._resolve_session_agent_runtime(
                    source=ctx.source, session_key=ctx.session_key, user_config=boundary_config,
                    assignment=assignment,
                )
            from gateway.attended_model import attended_request_context_too_large
            if commit and decision.switched_back and attended_request_context_too_large(
                agent, loop_state, model=model, runtime=runtime,
            ):
                log_decision = getattr(runner, "_log_attended_decision", None)
                if callable(log_decision):
                    log_decision(
                        ctx.session_key, ctx.source, action="keep", reason="context_too_large",
                    )
                else:
                    logger.info(
                        "attended decision: session=%s origin=%s action=keep reason=context_too_large",
                        ctx.session_key,
                        "automation" if str(getattr(getattr(ctx.source, "platform", None), "value", "")).lower()
                        in {"automation", "cron", "goal_wakeup", "kanban", "subagent", "system"}
                        else "user",
                    )
                agent._gateway_attended_decision = previous_decision
                _handoff(conversation={"last_attended_decision": previous_decision})
                return
            if commit and decision.switched_back:
                log_decision = getattr(runner, "_log_attended_decision", None)
                if callable(log_decision):
                    log_decision(
                        ctx.session_key, ctx.source, action="switch", reason="idle",
                        effort=decision.reasoning_effort or "none",
                    )
                _handoff(
                    conversation={"last_attended_decision": decision},
                    runner_state={"_last_attended_decision": decision},
                )
            current_route = (
                getattr(agent, "model", ""), getattr(agent, "provider", ""), getattr(agent, "api_mode", ""),
            )
            destination_route = (model, runtime.get("provider", ""), runtime.get("api_mode", ""))
            unavailable_emitted = False
            if decision.reason == "attended" and destination_route == current_route:
                consume_notice = getattr(runner, "_consume_pre_agent_fallback_notice", None)
                unavailable = consume_notice(ctx.session_key) if callable(consume_notice) else None
                emit_status = getattr(agent, "_emit_diagnostic_status", None)
                if unavailable and callable(emit_status):
                    emit_status(unavailable)
                    unavailable_emitted = True
            if destination_route != current_route:
                from agent.agent_runtime_helpers import switch_model
                switch_model(
                    agent, model, runtime.get("provider", agent.provider), runtime.get("api_key", ""),
                    runtime.get("base_url", ""), runtime.get("api_mode", ""), runtime.get("capabilities"),
                )
            if decision.reasoning_effort:
                # The committed decision already carries the destination effort. Re-resolving here
                # would read session state whose handoff to the gateway loop may not have landed yet,
                # and could pair the default model with the attended route's effort.
                from hermes_constants import parse_reasoning_effort
                agent.reasoning_config = parse_reasoning_effort(decision.reasoning_effort)
            else:
                agent.reasoning_config = runner._resolve_session_reasoning_config(
                    source=ctx.source, session_key=ctx.session_key, model=model,
                    user_config=boundary_config, assignment=assignment,
                )
            _persist_committed_route_marker()
            agent._gateway_attended_decision_key = desired_key
            if ctx.session_key:
                _handoff(conversation={"last_resolved_model": model})
            notices = parse_attended_config(ctx.user_config or {}).notices
            if decision.switched_back and not unavailable_emitted and notices:
                self._emit_persistent_attended_notice(format_attended_switch_back_notice(
                    model, parse_attended_config(ctx.user_config or {}).idle_revert_minutes,
                ))
            elif (
                notices
                and decision.reason == "attended"
                and not decision.new_session
                and not unavailable_emitted
                # Turn-start resolution may already have selected and announced this route.
                # A changed desired key alone does not mean the live model switched.
                and destination_route != current_route
                and not (previous_key and previous_key[-1])
            ):
                effort = decision.reasoning_effort or (agent.reasoning_config or {}).get("effort") or "none"
                self._emit_persistent_attended_notice(format_attended_switch_notice(
                    model, effort, idle_model=decision.idle_model,
                    idle_minutes=parse_attended_config(ctx.user_config or {}).idle_revert_minutes,
                ))
            if ctx.session_key:
                _handoff(conversation={"pending_attended_notice": None})
        except Exception:
            logger.warning("attended model refresh before API call failed; keeping current route", exc_info=True)

    def _wire_turn_agent_callbacks(self, agent, turn_route, reasoning_config,
                                   stream_delta_cb, interim_assistant_cb, want_interim_messages):
        """Per-message state (callbacks, reasoning, voice route) — never baked into the cached agent."""
        ctx = self._ctx
        runner = self._runner
        agent._notification_config = ctx.user_config
        agent._notification_platform = ctx.source.platform
        # ALWAYS attached (never gated to None): its body gates each event class, and subagent-
        # failure notices must fire even with tool_progress/thinking off.
        agent.tool_progress_callback = ctx.progress_callback
        # Discord's one-time voice ack and Slack's task cards both ride the authoritative start
        # callback, so neither infers identity from tool names.
        agent.tool_start_callback = (
            (ctx.native_tool_start_callback or ctx.voice_ack_callback)
            if (ctx._voice_ack_guild[0] is not None or ctx._native_slack_task_cards) else None
        )
        agent.tool_complete_callback = ctx.native_tool_complete_callback if ctx._native_slack_task_cards else None
        if self._approval_owner is not None:
            agent.tool_start_callback = self.combined_tool_start_callback
            agent.tool_complete_callback = self.combined_tool_complete_callback
        agent.step_callback = ctx._step_callback_sync if (ctx._hooks_ref.loaded_hooks or self._approval_owner is not None) else None
        agent.stream_delta_callback = stream_delta_cb
        agent.interim_assistant_callback = interim_assistant_cb if want_interim_messages else None
        from gateway.run_turn_progress import local_viewer_turn
        agent.status_callback, agent.notice_callback = ctx._status_callback_sync, (
            self._local_notice_callback_sync if local_viewer_turn(self) else self._notice_callback_sync)
        agent.reasoning_callback = None
        if self._approval_owner is not None:
            from gateway.session_api_turn import api_execution, wire_api_observers
            if api_execution.get() is not None:
                wire_api_observers(agent, self._approval_owner, want_interim_messages)
        agent.notice_clear_callback = self._local_notice_clear_callback_sync if local_viewer_turn(self) else None
        agent.event_callback = ctx._event_callback_sync
        agent.reasoning_config, agent.service_tier = reasoning_config, runner._service_tier
        agent._voice_turn_pending = ctx.voice_turn  # auxiliary.voice_chat route
        reasoning_loader = getattr(runner, "_session_reasoning_override", None)
        if callable(reasoning_loader):
            agent.reasoning_override = reasoning_loader(ctx.session_key)
        else:
            overrides = getattr(runner, "_session_reasoning_overrides", None) or {}
            agent.reasoning_override = overrides.get(ctx.session_key)
        expiry_loader = getattr(runner, "_session_service_tier_expiry", None)
        try:
            expiry_at, _ = expiry_loader(ctx.session_key) if callable(expiry_loader) else (0.0, 0.0)
        except (AttributeError, TypeError, ValueError):
            expiry_at = 0.0
        from agent.fast_mode import set_gateway_fast_expiry_state
        set_gateway_fast_expiry_state(agent, expiry_at, runner._service_tier)
        agent._pre_fallback_reasoning_override = None  # a live pick supersedes a set-aside one
        self._merge_turn_request_overrides(agent, turn_route)
        from gateway.session_surface import arm_surface_turn
        arm_surface_turn(agent, runner._consume_pending_turn_sidecar_notes(ctx.session_key), voice_turn=ctx.voice_turn)
        agent._gateway_before_api_call = lambda loop_state: self._apply_attended_before_api_call(
            agent, loop_state, commit=False,
        )
        agent._gateway_after_preflight = lambda loop_state: self._apply_attended_before_api_call(
            agent, loop_state, commit=True,
        )
        peek = getattr(runner, "_peek_session_state", None)
        _attended_state = peek(ctx.session_key) if (ctx.session_key and callable(peek)) else None
        agent._gateway_attended_decision = getattr(
            getattr(_attended_state, "conversation", None),
            "last_attended_decision", None,
        )
        agent._gateway_attended_decision_key = getattr(
            getattr(agent, "_gateway_attended_decision", None), "decision_key", None,
        )
        agent.background_review_callback, bg_release = self._make_bg_review_callbacks()
        # Register the release hook on the adapter so base.py's finally block fires it after the
        # main response is delivered.
        if ctx._status_adapter and ctx.session_key:
            if getattr(type(ctx._status_adapter), "register_post_delivery_callback", None) is not None:
                ctx._status_adapter.register_post_delivery_callback(ctx.session_key, bg_release, generation=ctx.run_generation)
            else:
                pdc = getattr(ctx._status_adapter, "_post_delivery_callbacks", None)
                if pdc is not None:
                    pdc[ctx.session_key] = bg_release
        # display.memory_notifications: off | on (generic "💾 Memory updated", default) | verbose.
        # `display:` present-but-null yields None, not the {} default (same `or {}` guard as
        # display_config.py / runtime_footer.py).
        mem_notif = (ctx.user_config.get("display") or {}).get("memory_notifications")
        if isinstance(mem_notif, bool):
            mem_notif = "on" if mem_notif else "off"
        agent.memory_notifications = str(mem_notif).lower() if mem_notif else "on"
        agent.clarify_callback = self._clarify_callback_sync
        # Thinking between tool calls is independent of tool_progress mode (Mattermost opts in
        # per platform so global scratch-text doesn't leak into threads).
        agent.thinking_progress = ctx._thinking_enabled
        if ctx.mute_notification_reply:
            # Controls and operational event/step callbacks remain wired. These
            # presentation callbacks are rebound on every next turn.
            agent.tool_progress_callback = None
            agent.tool_start_callback = None
            agent.tool_complete_callback = None
            # Keep diagnostic observers installed; concrete sinks veto display.
            agent.stream_delta_callback = None
            agent.interim_assistant_callback = None
            agent.thinking_progress = False
        ctx.agent_holder[0] = agent  # interrupt support
        if self._approval_owner is not None:
            authority, owner_id, generation = self._approval_owner
            authority.adopt_agent(owner_id, generation, agent)
        # The titler fires from the turn prologue, so attach the rename lane before the run.
        self._attach_session_title_callback(agent, ctx)
        # Publish turn ownership for /stop, /new, disconnect and shutdown interrupts; older session
        # processes are outside this baseline and remain alive.
        agent._gateway_turn_process_task_id, agent._gateway_turn_process_baseline = ctx.process_task_id, ctx.process_baseline
        ctx.tools_holder[0] = getattr(agent, "tools", None)  # transcript logging

    # ── blocking prompts from the agent thread (approval / clarify) ─────────────────────────

    def _close_native_stream_boundary(self, reason: str, placeholder: str | None = None, reopen: bool = False) -> bool:
        """Native-streaming platforms (e.g. WeCom): an interrupting interaction (approval or clarify
        prompt) must finalize the current stream first, or post-interaction output keeps updating the
        OLD bubble above the prompt. Runs on the agent thread; the consumer serializes via its queue."""
        sc = self._stream_consumer()
        if not (sc and getattr(sc, "_use_native_streaming", False)):
            return True
        cancelled_flag = None
        try:
            boundary = sc.close_for_approval_prompt(placeholder, reason=reason, reopen=reopen)
            # Returns (future, cancelled_flag) or just a future.
            if isinstance(boundary, tuple):
                boundary, cancelled_flag = boundary
            if not hasattr(boundary, "result"):
                return True
            ok = boundary.result(timeout=10)
            if not ok:
                logger.warning(
                    "%s boundary failed to close stream properly — "
                    "prompt may still appear in typing bubble", reason,
                )
            return bool(ok)
        except (TimeoutError, Exception) as err:
            if cancelled_flag is not None:
                cancelled_flag["cancelled"] = True
            logger.warning("%s boundary timed out or failed: %s", reason, err)
            return False

    async def _send_shared_clarify(self, entry, **kwargs):
        result = await self._ctx._status_adapter.send_clarify(**kwargs)
        if self._approval_owner is not None and result.success:
            authority, session_id, generation = self._approval_owner
            from gateway import local_viewer_events
            authority.register_clarify(session_id, generation, entry,
                                       timeout_s=local_viewer_events.prompt_timeout_s(self, "clarify"))
        return result

    def _clarify_callback_sync(self, questions) -> dict:
        """Answer the clarify tool's questions (clarify_tool's synchronous contract): one card per
        question, stop at the first the user never answers. The stream/typing re-arm waits for the
        last question — between two cards it only opens a bubble the next boundary closes."""
        from gateway.run_turn_runner_clarify_delivery import UNDELIVERED, UNDELIVERED_DECLINED, UNDELIVERED_NO_SURFACE
        from tools.clarify_gateway import CANCELLED, SKIPPED
        if _finite_viewer_turn():
            # `chat -q` / `-z`: the viewer detaches on the first prompt, so nobody can answer.
            # Answer headless at once, as main's `_single_query_clarify_callback` did in process.
            from gateway.session_finite import finite_clarify_reply
            return finite_clarify_reply()
        from gateway.session_cli_turn import clarify_batch_turn
        if clarify_batch_turn(self):
            # Main's batch panel (classic CLI, Ink TUI) shows every question at once: one request.
            from gateway.session_clarify_batch import ask_clarify_batch
            return ask_clarify_batch(self, questions)
        answers: dict[str, Any] = {}
        reply: dict[str, Any] = {"answers": answers, "outcome": "submitted"}
        last = len(questions) - 1
        # The typed-"skip" hint is for messaging adapters, where a chat reply answers the card. A local
        # viewer (TUI / Desktop / CLI) renders main's modal with its own skip control, unhinted.
        platform = getattr(getattr(self._ctx, "source", None), "platform", None)
        hint = "" if platform == Platform.LOCAL else f"\n{t('gateway.clarify.skip_hint')}"
        for index, entry in enumerate(questions):
            question = f"{entry['question']}{hint}"
            raw, answered = self._ask_clarify_question(
                question, entry["choices"], bool(entry["multi_select"]), rearm=index == last)
            if raw == CANCELLED:
                reply["outcome"] = "cancelled"
                break
            if not answered:
                # The surface's own no-answer text ("could not be delivered", "did not respond
                # within Nm") rides along as ``notice``: blank answers alone read as user
                # inactivity, which is the misreport #112684 describes for an undelivered card.
                undelivered = raw in (UNDELIVERED, UNDELIVERED_DECLINED, UNDELIVERED_NO_SURFACE)
                reply.update(outcome="undelivered" if undelivered else "timed_out", notice=raw)
                break
            answers[entry["qid"]] = None if raw == SKIPPED else raw
        return reply

    def _ask_clarify_question(self, question, choices, multi_select, rearm: bool = True) -> tuple[str, bool]:
        """One card: register, send, wait, then retire it (no answer) or re-arm (answer).
        Returns ``(response, answered)``; the caller decides what "no answer" means."""
        from gateway.run_turn_runner_clarify_delivery import (
            UNDELIVERED_NO_SURFACE, _clarify_send_then_wait, text_fallback_coro)
        from tools import clarify_gateway as clarify_mod
        import uuid
        ctx = self._ctx
        if not ctx._status_adapter:
            # Nothing can render the question: say so, or the batch's blank answers read as
            # user inactivity (#112684).
            return UNDELIVERED_NO_SURFACE, False
        session_key = ctx.session_key or ""
        clarify_id = uuid.uuid4().hex[:10]
        choices = list(choices) if choices else None
        send_kwargs = dict(
            chat_id=ctx._status_chat_id, question=question, choices=choices, clarify_id=clarify_id,
            session_key=session_key, metadata=ctx._status_thread_metadata,
        )

        def _text_fallback():
            """Schedule the plain-text prompt when the native card cannot render; None = no such path."""
            coro = text_fallback_coro(ctx._status_adapter, **send_kwargs)
            return None if coro is None else self._schedule(coro, "Clarify text fallback failed to schedule")
        entry = clarify_mod.register(
            clarify_id=clarify_id, session_key=session_key, question=question, choices=choices,
            multi_select=bool(multi_select),
        )
        # Unlike approval, clarify passes reopen=True so the continuation re-opens a native stream
        # below the question; if the re-seed fails the consumer degrades to send() automatically.
        self._close_native_stream_boundary("Clarify", t("gateway.clarify.native_stream_placeholder"), reopen=True)
        # Pause typing: a "thinking..." status must not obscure the prompt or block an "Other" reply
        # on platforms that disable input while typing (Slack Assistant).
        with suppress(Exception):
            pause_typing_for_prompt(
                ctx._status_adapter, ctx.session_key, ctx._status_chat_id,
                typing_metadata_for_source(ctx.source, ctx.event_message_id))
        # Ordering barrier: flush buffered assistant prose BEFORE the poll, which goes out on a
        # separate agent-thread-blocking path and would otherwise render ABOVE its own explanation.
        # Best-effort + short timeout so the agent thread never hangs if the consumer isn't running.
        flush = getattr(self._stream_consumer(), "flush_pending_sync", None)
        try:
            if callable(flush):
                flush(timeout=3.0)
        except Exception:
            logger.debug("Stream-consumer flush before clarify prompt failed", exc_info=True)
        fut = self._schedule(
            self._send_shared_clarify(entry, **send_kwargs),
            "Clarify send failed to schedule",
        )
        # Boundary rule (see _approval_send_outcome): a send timeout is AMBIGUOUS — the card may
        # have posted with a late ack. Only a definitive failure tears down the registration;
        # ambiguous falls through to the bounded wait so a late reply resolves. A definitive
        # failure — immediate or late — retries once as plain text before giving up.
        response, answered = _clarify_send_then_wait(
            fut, clarify_id=clarify_id, session_key=session_key, clarify_mod=clarify_mod,
            fallback=_text_fallback)
        # Branch on the explicit flag, never on the text: a real answer can start with '[' (a
        # "[A] staging" label, "[urgent] ..." free text) and must not be mistaken for a sentinel.
        if not answered:
            # No answer arrived (timeout, /new, run end): retire the native card so it stops
            # looking answerable. Adapters without a persistent card have no such method.
            retire = getattr(type(ctx._status_adapter), "retire_clarify_card", None)
            if callable(retire):
                self._schedule(
                    retire(ctx._status_adapter, clarify_id, _clarify_expired_notice()),
                    "Clarify card retire failed to schedule")
        elif rearm:
            # Reopen typing IMMEDIATELY, not on the LLM's first post-answer token (native streaming
            # otherwise re-seeds lazily on the first delta: ~48s of dead air). request_reopen_seed is
            # a no-op outside the reopen-pending native state.
            sc = self._stream_consumer()
            if sc is not None:
                try:
                    sc.request_reopen_seed()
                except Exception:
                    logger.debug("request_reopen_seed after clarify answer failed", exc_info=True)
            try:
                resume_typing_after_prompt(
                    ctx._status_adapter, ctx.session_key, ctx._status_chat_id,
                    typing_metadata_for_source(ctx.source, ctx.event_message_id))
            except Exception:
                logger.debug("resume_typing_for_chat after clarify answer failed", exc_info=True)
        return response, answered

    def _approval_notify_sync(self, approval_data: dict) -> None:
        owner = self._approval_owner
        if owner is not None:
            authority, session_id, generation = owner
            authority.check_approval_generation(session_id, generation)
        # A native decline must finish before any observer can authorize work.
        self._render_approval_sync(approval_data)
        if owner is not None:
            from gateway import local_viewer_events
            authority.register_approval(
                session_id, generation, self._ctx.session_key, approval_data,
                timeout_s=local_viewer_events.prompt_timeout_s(self, "approval"),
            )

    def _render_approval_sync(self, approval_data: dict) -> None:
        """Send the approval request from the agent thread: the adapter's interactive button
        approvals (``send_exec_approval``) when available, else plain text with ``/approve`` steps."""
        from gateway.run import _approval_send_outcome, _format_exec_approval_fallback, _interim_metadata, _redact_approval_command
        from gateway.run_turn_runner_approval_settle import register_timeout_notice
        ctx = self._ctx
        adapter = ctx._status_adapter
        # Slack's assistant_threads_setStatus disables the compose box, so the user can't type
        # /approve while "is thinking..." shows. Pausing stops _keep_typing re-setting it; resumed
        # in approve/deny.
        pause_typing_for_prompt(
            adapter, ctx.session_key, ctx._status_chat_id,
            typing_metadata_for_source(ctx.source, ctx.event_message_id))
        self._close_native_stream_boundary("Approval")
        # Redact credentials before display: the raw command string can carry secrets. Both the button and plain-text paths use this value.
        cmd = _redact_approval_command(approval_data.get("command", ""))
        desc = approval_data.get("description") or ea_default_reason_text()
        flags = {k: approval_data.get(k, d) for k, d in (("allow_permanent", True), ("allow_session", True), ("smart_denied", False))}
        # Check the *class*, not the instance — MagicMock auto-creates attributes in tests.
        if _renders_exec_approval_buttons(type(adapter)):
            try:
                fut = self._schedule(
                    adapter.send_exec_approval(
                        chat_id=ctx._status_chat_id, command=cmd, session_key=ctx.session_key or "",
                        description=desc, metadata=ctx._status_thread_metadata, **flags,
                    ),
                    "send_exec_approval scheduling error",
                )
                if fut is None:
                    raise RuntimeError("send_exec_approval: loop unavailable")
                outcome = _approval_send_outcome(fut, timeout=15)
                if outcome == "sent":
                    # Without this, a card whose timer runs out keeps live buttons and nobody
                    # learns the command did NOT run (only the TUI registered a settle hook).
                    register_timeout_notice(
                        self, approval_data, command=cmd,
                        card_message_id=getattr(fut.result(timeout=0), "message_id", None))
                    return
                if outcome == "ambiguous":
                    # Timeout ≠ failure: the card may have posted with a late ack. The prompt
                    # registration stays alive so a tap still resolves; re-sending made duplicate
                    # cards + orphaned "/approve: nothing pending".
                    logger.warning(
                        "Button-based approval send timed out — treating "
                        "as possibly-delivered (no re-send; the prompt "
                        "stays armed for a late tap)"
                    )
                    return
                if outcome == "declined":
                    # P5(b): the connector AUTHORIZED this destination and
                    # refused it. The text fallback below re-sends the same
                    # content to the same chat, which would turn a refused
                    # button card into a delivered plain-text one — the exact
                    # leak the egress guard exists to stop. A decline is
                    # definitive, so unlike `ambiguous` the registration is
                    # torn down; unlike `failed`, nothing is re-sent.
                    logger.warning(
                        "Button-based approval DECLINED by the connector's "
                        "egress guard — not falling back to text (the "
                        "destination is not approved for this connection)"
                    )
                    # RAISE, do not return. This function is the notify_cb for
                    # `_await_gateway_decision`, which already has a correct
                    # undeliverable path: a raising notify drops the queue entry
                    # and returns `notify_failed`, unblocking the tool. Returning
                    # quietly suppressed the text fallback (right) but left the
                    # CENTRAL approval entry pending (wrong) — the dangerous
                    # command then blocked until the approval timeout. My earlier
                    # comment claimed the registration was torn down; only the
                    # adapter's private prompt map was.
                    raise _ExecApprovalDeclined(
                        "exec approval undeliverable: connector egress declined "
                        "this destination"
                    )
                logger.warning("Button-based approval failed (send returned error), falling back to text")
            except _ExecApprovalDeclined:
                # Must escape this handler: the fallback below is a text send to
                # the destination the connector just refused.
                raise
            except Exception as e:
                logger.warning("Button-based approval failed, falling back to text: %s", e)
        # Plain-text prompt with the adapter's typed prefix (e.g. `!approve`): typed "/" is blocked
        # in Slack threads and reserved by Matrix clients.
        msg = _format_exec_approval_fallback(cmd, desc, getattr(adapter, "typed_command_prefix", "/"), **flags)
        try:
            # Mark as approval prompt: WeCom routes it through the control lane and Telegram pushes it
            # in "important" mode (#132516). Never ``notify`` — A2A reads that as the turn-final reply.
            metadata = {**(ctx._status_thread_metadata or {}), "is_approval_prompt": True}
            fut = self._schedule(
                adapter.send(ctx._status_chat_id, msg, metadata=_interim_metadata(metadata)), "Approval text-send scheduling error",
            )
            if fut is not None:
                fut.result(timeout=15)
                # No card to edit on the text path: the prompt has no buttons to drop and carries
                # the /approve instructions, so the timeout notice is posted as a new message.
                register_timeout_notice(self, approval_data, command=cmd, card_message_id=None)
        except Exception as e:
            logger.error("Failed to send approval request: %s", e)

    # ── run_sync phases ─────────────────────────────────────────────────────────────────────

    def _load_turn_history(self, agent, reused_cached_agent):
        from gateway.run import (
            _build_gateway_agent_history, _collect_history_media_paths, _message_timestamps_enabled,
            _select_cached_agent_history,
        )
        ctx = self._ctx
        from gateway.session_api_turn import api_execution
        api = api_execution.get()
        if api is not None and api['history'] is not None:
            from gateway.run import _collect_history_media_paths
            history = api['history']
            tail = history[-1] if history else None
            if api.get('resume_unanswered') and isinstance(tail, dict) and tail.get('role') == 'user':
                # The door's re-run continues the row its failed attempt persisted (#115325). Only a
                # bare user tail: a tool suffix is completed work the re-run must still see.
                from agent.session_persistence import adopt_unanswered_turn
                history = list(history)
                adopt_unanswered_turn(history, api['content'], agent)
            return history, None, _collect_history_media_paths(history)
        # Transcript rows ({role, content, timestamp}) lose timestamps; interrupt-path agent messages
        # (tool_calls/tool_call_id/reasoning) pass through intact so the API sees valid assistant→tool
        # sequences. Telegram observed=True rows are withheld from replayable history and attached to
        # the current addressed message as API-only context.
        agent_history, observed_group_context = _build_gateway_agent_history(
            ctx.history, channel_prompt=ctx.channel_prompt, inject_timestamps=_message_timestamps_enabled(ctx.user_config),
        )
        # FTS write-corruption guard: if persistence failed silently the reloaded transcript is stale
        # while the SAME cached agent still holds the live conversation (same-session amnesia). Only
        # for a reused agent bound to this exact session_id.
        # Replacing the live transcript with that shorter copy causes immediate same-session amnesia. See
        # #50502.
        if reused_cached_agent and getattr(agent, "session_id", None) == ctx.session_id:
            selected = _select_cached_agent_history(agent_history, getattr(agent, "_session_messages", None))
            # Consecutive lagging turns per session (cleared on /new): one lag is a blip, a streak
            # is the #114266 write outage and must escalate past a repeating WARNING.
            streaks = getattr(self._runner, "_transcript_lag_streaks", None)
            if streaks is None:  # tests may build bare runners
                streaks = self._runner._transcript_lag_streaks = {}
            if selected is not agent_history:
                streak = streaks[ctx.session_key] = streaks.get(ctx.session_key, 0) + 1
                log = logger.error if streak >= _TRANSCRIPT_LAG_ESCALATION_TURNS else logger.warning
                log(
                    "Persisted transcript lagged live cached history for "
                    "session %s (disk=%d, memory=%d, consecutive_turns=%d); preserving live "
                    "conversation context (possible FTS write corruption)%s",
                    ctx.session_key, len(agent_history), len(selected), streak,
                    "; state.db is not receiving this session's writes and needs operator attention"
                    if streak >= _TRANSCRIPT_LAG_ESCALATION_TURNS else "",
                )
                # The live history bypassed _build_gateway_agent_history's cleanup — re-apply
                # the full canonicalization so no replay transform can slip through.
                agent_history = canonicalize_replay_history(selected)
            else:
                streaks.pop(ctx.session_key, None)
        # MEDIA paths already in history are excluded from this turn's extraction (compression-safe).
        return agent_history, observed_group_context, _collect_history_media_paths(agent_history)

    def _prepend_pending_note(self, attr: str) -> None:
        """Consume a one-shot per-session note (model switch, /reload-skills) into the NEXT user
        message. Nothing hits the transcript out-of-band, so alternation stays intact."""
        ctx = self._ctx
        notes = getattr(self._runner, attr, None)
        note = notes.pop(ctx.session_key, None) if notes and ctx.session_key and ctx.session_key in notes else None
        if note:
            ctx.message = note + "\n\n" + ctx.message

    def _resume_restart_policy(self) -> str:
        """Resolved restart recovery policy for this turn's platform: per-platform
        ``extra.restart_resume_policy`` > global ``gateway.restart_resume_policy`` > adapter default
        (event platforms continue because nobody is present to answer; others ask)."""
        from gateway.run import resolve_restart_resume_policy
        return resolve_restart_resume_policy(
            getattr(self._runner, "config", None), self._runner._delivery_adapter_for(self._ctx.source))

    def _prepare_turn_message(self, agent_history):
        """Prepend recovery/notice guidance to ``ctx.message``.

        Returns (persist_user_message_override, persist_user_timestamp_override): real user text is
        kept separate from API-only recovery guidance so stale guidance never replays as user text.
        """
        from gateway.run import (
            _auto_continue_freshness_window, _is_fresh_gateway_interruption,
            _last_transcript_timestamp, _prepare_resume_pending_message, build_resume_recovery_note,
        )
        ctx = self._ctx
        persist_override: Optional[Any] = ctx.persist_user_message
        from gateway.session_cli_turn import cli_turn
        api_text = cli_turn(getattr(ctx, "inbound_message_id", None)).get('api_text')
        if api_text is not None:
            # The classic CLI's model-facing message (voice prefix, its own model-switch and
            # skills-reload notes); the admitted text stays the persisted user row.
            clean_text = persist_override if persist_override is not None else ctx.message
            persist_override = clean_text
            ctx.message = api_text
        self._prepend_pending_note("_pending_model_notes")
        # Auto-continue: history ending with a tool result means the previous turn was cut off
        # (restart, crash, SIGTERM). Session-level resume_pending (drain-timeout shutdown) uses
        # stronger reason-aware wording that subsumes this case. Both gate on the age of
        # ``history[-1]`` (not agent_history, which stripped tool-row timestamps); no stamp = fresh.
        window = _auto_continue_freshness_window()
        interruption_is_fresh = _is_fresh_gateway_interruption(_last_transcript_timestamp(ctx.history), window_secs=window)
        entry = None
        if ctx.session_key:
            with suppress(Exception):
                entry = self._runner.session_store._entries.get(ctx.session_key)
        resume_pending = entry is not None and getattr(entry, "resume_pending", False)
        resume_reason = (getattr(entry, "resume_reason", None) or "restart_timeout") if resume_pending else None
        # resume_pending freshness ALSO uses the restart watchdog's ``last_resume_marked_at`` (the true
        # interruption stamp): the transcript clock can be hours older for an active thread, and the
        # startup auto-resume turn has empty text, so gating on it alone yields a blank user message.
        mark_is_fresh = resume_pending and _is_fresh_gateway_interruption(
            getattr(entry, "last_resume_marked_at", None), window_secs=window,
        )
        if resume_pending and (interruption_is_fresh or mark_is_fresh):
            # Empty message = the startup auto-resume turn; there is no NEW user message.
            ctx.message, persist_override = _prepare_resume_pending_message(
                resume_reason, ctx.message, restart_resume_policy=self._resume_restart_policy(),
            )
        elif agent_history and agent_history[-1].get("role") == "tool" and interruption_is_fresh:
            persist_override = ctx.message
            ctx.message = (
                "[System note: A new message has arrived. The conversation "
                "history contains pending tool outputs from an interrupted turn. "
                "IGNORE those pending results. Address the user's NEW message "
                "below FIRST. Do NOT re-execute old tool calls from the history.]\n\n"
                + ctx.message
            )
        self._prepend_pending_note("_pending_skills_reload_notes")
        # Safety net: a startup auto-resume event carries empty text; if the resume_pending branch
        # did not fire (freshness signals disagreed, marker cleared) we must NOT hand the model a blank
        # user turn. Restricted to resume_pending sessions so caption-less image turns are untouched.
        if isinstance(ctx.message, str) and not ctx.message.strip() and resume_pending:
            ctx.message = build_resume_recovery_note(
                resume_reason, "", restart_resume_policy=self._resume_restart_policy())
        return persist_override, ctx.persist_user_timestamp

    def _native_image_run_message(self):
        """Wrap the user turn as an OpenAI-style multimodal content list when
        _prepare_inbound_message_text buffered image paths; consume-and-clear so later turns on the
        same runner never re-attach stale images. Falls back to plain text when nothing is readable."""
        ctx = self._ctx
        from gateway.session_api_turn import api_execution
        api = api_execution.get()
        if api is not None and isinstance(api.get('content'), list):
            return api['content']
        native_imgs = self._runner._consume_pending_native_image_paths(ctx.session_key)
        if not native_imgs:
            return ctx.message
        try:
            from agent.image_routing import build_native_content_parts
            parts, skipped = build_native_content_parts(ctx.message, native_imgs)
            if skipped:
                logger.warning("Native image attachment: skipped %d unreadable path(s): %s", len(skipped), skipped)
            if any(p.get("type") == "image_url" for p in parts):
                return parts
        except Exception as exc:
            logger.warning("Native image attachment failed, falling back to text: %s", exc)
        return ctx.message

    def _run_conversation_with_approval(self, agent, agent_history, observed_group_context,
                                        persist_user_message_override, persist_user_timestamp_override):
        """Run the turn with the per-session gateway approval callback registered: dangerous-command
        approval blocks the agent thread (mirrors CLI input()); the callback bridges sync→async."""
        from gateway.run import _wrap_current_message_with_observed_context
        from tools.approval import register_gateway_notify, unregister_gateway_notify
        from tools.approval_context import reset_current_session_key, set_current_session_key
        ctx = self._ctx
        session_key = ctx.session_key or ""
        token = set_current_session_key(session_key)
        register_gateway_notify(session_key, self._approval_notify_sync)
        try:
            api_message = _wrap_current_message_with_observed_context(self._native_image_run_message(), observed_group_context)
            # Turn provenance is already task-local in HERMES_TURN_ORIGIN. Do not add it to
            # run_conversation's call shape, because gateway test doubles and provider adapters
            # intentionally implement the older stable signature.
            kwargs = {"conversation_history": agent_history, "task_id": ctx.session_id}
            from gateway.session_api_turn import api_execution
            api = api_execution.get()
            if api is not None and api.get('relay_metadata') and _accepts_keyword(agent.run_conversation, "relay_metadata"):
                kwargs["relay_metadata"] = api['relay_metadata']
            if _accepts_keyword(agent.run_conversation, "turn_author"):
                from gateway.session_ingress import admission_author
                kwargs["turn_author"] = (api.get('turn_author') if api is not None else
                    admission_author.get() or {"id": ctx.source.user_id or None, "name": ctx.source.user_name or None,
                                               "is_bot": bool(getattr(ctx.source, "is_bot", False))})
            if ctx.title_user_message is not None:
                kwargs["title_user_message"] = ctx.title_user_message
            if persist_user_message_override is not None:
                kwargs["persist_user_message"] = persist_user_message_override
            elif observed_group_context:
                kwargs["persist_user_message"] = ctx.message
            if ctx.persist_user_display_kind:
                # Internal self-injected turn: type the persisted user row so UIs render it as a
                # timeline notice, not a user bubble (stripped from provider payloads downstream).
                kwargs["persist_user_display_kind"] = ctx.persist_user_display_kind
            if ctx.persist_user_display_metadata:
                kwargs["persist_user_display_metadata"] = ctx.persist_user_display_metadata
            if ctx.moa_config is not None:
                kwargs["moa_config"] = ctx.moa_config
            from gateway.session_onboarding import arm_onboarding_turn
            prelude = arm_onboarding_turn(agent, self._approval_owner, agent_history)
            if prelude is not None and _accepts_keyword(agent.run_conversation, "prelude"):
                kwargs["prelude"] = prelude
            if persist_user_timestamp_override is not None:
                kwargs["persist_user_timestamp"] = persist_user_timestamp_override
            # The raw inbound id (not event_message_id, the reply anchor) rides the persisted user
            # turn. Recovered internal follow-ups use a dedicated recovery key here; it must never
            # become event.message_id/source.message_id or reach transport routing.
            persist_id = getattr(ctx, "inbound_message_id", None) or ctx.recovery_id
            if persist_id is not None:
                kwargs["persist_user_platform_id"] = str(persist_id)
            from agent.notification_presentation import notification_turn
            from gateway.session_context import (
                current_message_reaction_acknowledged, reaction_acknowledgement, scoped_turn_message,
            )
            from gateway.session_results import execution_result
            captured = execution_result.get()
            before = (getattr(agent, 'session_prompt_tokens', 0) or 0,
                      getattr(agent, 'session_completion_tokens', 0) or 0)
            with notification_turn(agent, muted=ctx.mute_notification_reply, session_id=ctx.session_id or ""), silence_retry_prompt(agent, ctx), scoped_turn_message(ctx.source, ctx.inbound_message_id or getattr(ctx.source, "message_id", None)), reaction_acknowledgement():
                result = agent.run_conversation(api_message, **kwargs)
                result["acknowledged_by_reaction"] = current_message_reaction_acknowledged()
            if captured is not None:
                incoming = max(0, (getattr(agent, 'session_prompt_tokens', 0) or 0) - before[0])
                outgoing = max(0, (getattr(agent, 'session_completion_tokens', 0) or 0) - before[1])
                captured['usage'] = {'input_tokens': incoming, 'output_tokens': outgoing,
                                     'total_tokens': incoming + outgoing}
                from gateway.session_api_turn import settle_api_runtime_lock
                settle_api_runtime_lock(agent, result, captured)
            return result
        finally:
            unregister_gateway_notify(session_key)
            # Cancel pending clarify entries so blocked agent threads don't hang past the end of the
            # run (interrupt, completion, gateway shutdown). Idempotent.
            with suppress(Exception):
                from tools.clarify_gateway import clear_session
                clear_session(session_key)
            reset_current_session_key(token)

    def _finish_stream_consumer(self, result, agent_history, stream_consumer):
        ctx = self._ctx
        # Canonicalize a model-emitted computer-use screenshot path at the common result boundary so
        # the streaming finalizer and the non-streaming delivery path see the same response.
        if isinstance(result, dict) and isinstance(result.get("final_response"), str):
            result["final_response"] = repair_explicit_computer_use_media_paths(
                result["final_response"], result.get("messages", []), history_offset=len(agent_history),
            )
        from gateway.stream_recovery import mark_recovered_reply
        # A marked delivered copy: the agent's own result (and its messages) stay unlabelled.
        result = mark_recovered_reply(result, stream_consumer)
        ctx.result_holder[0] = result
        if stream_consumer is None:
            return result
        # Pass final_response as the authoritative finalize payload: it includes post-stream
        # augmentation (verifier footer, explainer) the accumulator never saw. Adopt ONLY a genuinely
        # completed final: interrupt paths return {interrupted: True, completed: False} with a
        # DIAGNOSTIC final_response — adopting it would seal the partial answer over with the
        # diagnostic AND suppress the gateway's own error delivery.
        _final_for_stream = None
        if (
            isinstance(result, dict) and not result.get("failed") and not result.get("interrupted")
            and result.get("completed") is not False
        ):
            fr = result.get("final_response")
            if isinstance(fr, str) and fr.strip() and fr != "(empty)":
                _final_for_stream = fr
        if _final_for_stream is None:
            stream_consumer.finish()
            return result
        # Duck-type safe: test doubles / older consumers may expose a zero-arg finish().
        try:
            stream_consumer.finish(_final_for_stream)
        except TypeError:
            stream_consumer.finish()
        return result

    def _restore_telegram_thread_id_after_split(self, agent_session_id) -> None:
        """Telegram DM whose source.thread_id was lost in the session split (synthetic/recovered
        event): restore it from the binding so _thread_metadata_for_source yields the right
        message_thread_id instead of the General thread (non-fatal)."""
        ctx = self._ctx
        try:
            # run_sync is off-loop (executor); sync DB is fine.
            binding = self._runner._session_db._db.get_telegram_topic_binding_by_session(session_id=agent_session_id)
            if binding and binding.get("thread_id"):
                ctx.source.thread_id = str(binding["thread_id"])
                logger.debug(
                    "Restored source.thread_id=%s from binding after session split %s → %s",
                    ctx.source.thread_id, ctx.session_id, agent_session_id,
                )
        except Exception:
            logger.debug("Failed to restore thread_id from binding after session split", exc_info=True)

    def _sync_session_after_run(self, agent_history):
        """Sync session_id right after run_conversation(): compression can rotate before a follow-up
        model call fails, and the failure return must still point at the compressed child.
        Returns (compacted_in_place, effective_session_id, effective_history_offset)."""
        ctx = self._ctx
        runner = self._runner
        agent = ctx.agent_holder[0]
        # In-place compaction compacts the transcript WITHOUT rotating the id, so the id-change diff
        # can't see it; compress_context() sets this flag and the gateway re-baselines as for a split.
        compacted_in_place = bool(getattr(agent, "_last_compaction_in_place", False)) if agent else False
        agent_session_id = getattr(agent, 'session_id', ctx.session_id) if agent else ctx.session_id
        session_was_split = bool(agent and ctx.session_key and agent_session_id != ctx.session_id)
        if session_was_split:
            logger.info("Session split detected: %s → %s (compression)", ctx.session_id, agent_session_id)
            entry = runner.session_store._entries.get(ctx.session_key)
            persisted = False
            if entry:
                entry_session_id = getattr(entry, "session_id", None)
                if not ctx._run_still_current():
                    logger.info(
                        "Skipping session split sync for stale run %s — "
                        "generation %s is no longer current",
                        ctx.session_key or "?", ctx.run_generation,
                    )
                elif entry_session_id == agent_session_id:
                    persisted = True
                elif entry_session_id != ctx.session_id:
                    logger.info(
                        "Skipping session split sync for %s because the "
                        "session binding moved from %s to %s before "
                        "compression finished",
                        ctx.session_key or "?", ctx.session_id, entry_session_id,
                    )
                else:
                    entry.session_id = agent_session_id
                    runner.session_store._save()
                    runner.session_store._record_gateway_session_peer(agent_session_id, ctx.session_key, ctx.source)
                    persisted = True
            # Only after this run published its split — a stale /stop→/new predecessor must not
            # mutate routing state.
            if persisted:
                src = ctx.source
                if (
                    getattr(src, "platform", None) == Platform.TELEGRAM and getattr(src, "chat_type", None) == "dm"
                    and getattr(src, "thread_id", None) is None and runner._session_db is not None
                ):
                    self._restore_telegram_thread_id_after_split(agent_session_id)
                runner._sync_telegram_topic_binding(src, entry, reason="agent-run-compression")
        from gateway.session_cli_turn import cli_turn
        if not cli_turn(getattr(ctx, "inbound_message_id", None)).get("model_override"):
            runner._sync_session_model_from_agent(agent_session_id, agent)
        # history_offset=0 whenever the agent's message list lost the original history prefix
        # (split OR in-place compaction): the returned `messages` is the compacted set, persist all
        # of it; slicing past the pre-compaction length would drop everything.
        offset = 0 if (session_was_split or compacted_in_place) else len(agent_history)
        return compacted_in_place, agent_session_id, offset

    def _combined_ephemeral_prompt(self) -> str:
        """Platform context + YAML channel_prompts hint + channel_overrides system_prompt (or global
        ephemeral) + the gateway ephemeral prompt."""
        ctx = self._ctx
        from gateway.session_api_turn import api_execution
        api = api_execution.get()
        if api is not None:
            return api['settings'].get('ephemeral_system_prompt') or ''
        combined = ctx.context_prompt or ""
        for extra in (
            (ctx.channel_prompt or "").strip(),
            self._runner._get_system_prompt_for_channel(
                ctx.source.platform, ctx.source.chat_id or "", thread_id=getattr(ctx.source, "thread_id", None),
                parent_id=getattr(ctx.source, "parent_chat_id", None),
            ),
        ):
            if extra:
                combined = (combined + "\n\n" + extra).strip()
        return combined

    def _append_auto_media_tags(self, final_response: str, result, agent_history, history_media_paths) -> str:
        """Append MEDIA:<path> tags from tool results (e.g. TTS) that the model's final text omits, so
        extract_media() delivers each file once. Scoped to THIS turn (slice at len(agent_history)) so
        a stale MEDIA: path from an earlier turn never rides a later reply; the history-path dedup is
        the secondary guard — and the sole one when mid-run compression shrank the list."""
        from gateway.run import _collect_auto_append_media_tags
        if "MEDIA:" in final_response:
            return final_response
        # Scan tool results for MEDIA:<path> tags that need to be delivered as native audio/file
        # attachments. The TTS tool embeds MEDIA: tags in its JSON response, but the model's final text
        # reply usually doesn't include them. We collect unique tags from tool results and append any that
        # aren't already present in the final response, so the adapter's extract_media() can find and
        # deliver the files exactly once. Scope the scan to THIS turn's tool results only. ``agent_history``
        # was passed into run_conversation as ``conversation_history``, so the agent's returned ``messages``
        # list is ``agent_history`` followed by the messages produced this turn. Slicing at
        # ``len(agent_history)`` isolates the current turn precisely, so a stale MEDIA: path emitted by a
        # tool several turns earlier (still present in the full message list) can never leak onto a later
        # text-only reply. (Fixes #34608) Path-based deduplication against _history_media_paths (collected
        # before run_conversation) is retained as a secondary guard. It is also the sole guard on the
        # fallback branch taken when mid-run context compression shrinks the message list below the original
        # history length, preserving the compression-safe behaviour of #160.
        media_tags, has_voice_directive = _collect_auto_append_media_tags(
            result.get("messages", []), history_offset=len(agent_history), history_media_paths=history_media_paths,
        )
        if not media_tags:
            return final_response
        unique_tags = (["[[audio_as_voice]]"] if has_voice_directive else []) + list(dict.fromkeys(media_tags))
        return final_response + "\n" + "\n".join(unique_tags)

    def run_sync(self):
        from contextlib import ExitStack
        from gateway.session_policy import bind_turn_workspace, policy_for_source, policy_scope, workspace_unavailable_result
        from gateway.session_api_turn import api_policy_scope
        from gateway.session_authorities import active_authority
        from hermes_state_runtime import RuntimeStoreError
        policy = policy_for_source(self._runner, self._ctx.source)
        with ExitStack() as scope:
            try:
                scope.enter_context(policy_scope(policy, authority=active_authority(self._runner)))
            except RuntimeStoreError as exc:
                if exc.reason != 'workspace_unavailable':
                    raise
                result = workspace_unavailable_result(policy)
            else:
                scope.enter_context(api_policy_scope())
                bind_turn_workspace(policy, self._ctx.session_id)
                result = self._run_sync_scoped()
            from gateway.session_results import execution_result
            captured = execution_result.get()
            if captured is not None:
                cli_fields = self.__dict__.pop("_cli_result_fields", None)
                captured['result'] = (
                    {**result, **cli_fields, **({'response_previewed': True} if result.get('response_previewed') else {})}
                    if cli_fields else result
                )
            return result

    def _run_sync_scoped(self):
        """Executor-thread body of the turn; returns the gateway result dict.

        The turn message lives on the shared TurnContext (``ctx.message``) so ``_run_agent_inner`` sees
        every rebind. session_key propagates via contextvars (_set_session_env / set_current_session_key)
        — never os.environ["HERMES_SESSION_KEY"], which would misroute approvals across sessions.
        """
        from gateway.run import _current_max_iterations
        ctx = self._ctx
        runner = self._runner
        from gateway.session_policy import policy_for_source
        policy = policy_for_source(runner, ctx.source)
        if policy and policy.yolo and ctx.session_key:
            from tools.approval_yolo import apply_launch_yolo
            apply_launch_yolo(ctx.session_key)
        # Platform.LOCAL ("local") maps to the "cli" hint key the agent understands.
        # session_key is propagated via contextvars in _set_session_env() (_SESSION_KEY) and via
        # set_current_session_key() (_approval_session_key) below — both concurrency-safe and inherited by
        # tool worker threads. We deliberately do NOT write os.environ["HERMES_SESSION_KEY"] here:
        # os.environ is process-global, so concurrent gateway sessions (e.g. two Discord threads) would
        # clobber each other's value, and a tool thread whose contextvar is unset would fall back to
        # os.environ and read the wrong session key — misrouting command-approval prompts to the wrong
        # thread (#24100). The non-gateway surfaces don't depend on this write: CLI and cron bind the
        # session via contextvars (set_current_session_key / session context), and only the TUI slash-worker
        # *subprocess* exports HERMES_SESSION_KEY (from its own --session-key argv, a separate process) — so
        # removing this in-process gateway write does not affect any of them.
        from gateway.session_policy import policy_for_source
        policy = policy_for_source(runner, ctx.source)
        if policy and policy.yolo and ctx.session_key:
            from tools.approval_yolo import apply_launch_yolo
            apply_launch_yolo(ctx.session_key)
        platform_key = policy.platform if policy else ("cli" if ctx.source.platform == Platform.LOCAL else ctx.source.platform.value)
        combined_ephemeral = self._combined_ephemeral_prompt()
        if policy and policy.skills_prompt:
            combined_ephemeral = "\n\n".join(p for p in (combined_ephemeral, policy.skills_prompt) if p)
        max_iterations = policy.max_turns if policy else _current_max_iterations()
        from gateway.hosted_room_execution_policy import current_room_execution_policy
        room = current_room_execution_policy()
        if room is not None:
            max_iterations = room.max_iterations
            ctx.enabled_toolsets = list(room.enabled_toolsets)
        try:
            model, runtime_kwargs = runner._resolve_session_agent_runtime(
                source=ctx.source, session_key=ctx.session_key, user_config=ctx.user_config,
                assignment=getattr(self, "_model_assignment", None),
            )
            # Stashed by _resolve_session_agent_runtime when the primary's credentials failed and a
            # fallback was resolved before any agent exists (#74349); one-shot per turn.
            consume_notice = getattr(runner, "_consume_pre_agent_fallback_notice", None)
            if callable(consume_notice) and getattr(consume_notice, "__self__", None) is runner:
                pending_fallback_notice = consume_notice(ctx.session_key)
            else:
                pending_fallback_notice = getattr(runner, "_pre_agent_fallback_notice", None)
                runner._pre_agent_fallback_notice = None
            from gateway.session_api_turn import prepare_api_runtime
            model, runtime_kwargs = prepare_api_runtime(model, runtime_kwargs)
            if policy and policy.model and not pending_fallback_notice:
                # The frozen route's model, unless resolution just fell back: the fallback entry's
                # model is the one this agent must send (#112600).
                from gateway.session_authorities import active_authority
                model = policy.current_model(active_authority(runner))
            # The classic CLI's `/model --once` / `/moa`: this admission only, nothing persisted.
            from gateway.session_cli_turn import one_turn_route
            model, runtime_kwargs = one_turn_route(self, policy, model, runtime_kwargs)
            logger.debug(
                "run_agent resolved: model=%s provider=%s session=%s",
                model, runtime_kwargs.get("provider"), ctx.session_key or "",
            )
        except Exception as exc:
            # Model/credential resolution failed before the turn began. Keep the raw cause in logs,
            # but return a failed turn with a stable user-facing recovery message and reason.
            logger.warning("Model resolution failed for session %s: %s", ctx.session_key or "", exc, exc_info=True)
            from agent.turn_failure_copy import stamp_failure
            text, reason, retryable = _resolution_failure_reply(exc, ctx.source.platform)
            return stamp_failure({"final_response": text, "messages": [], "api_calls": 0, "tools": [],
                                  "failed": True, "completed": False, "error": str(exc)}, reason, retryable)
        from gateway.session_authorities import active_authority
        from gateway.session_worker_construct import session_routing
        pr = session_routing(active_authority(runner), policy) if policy else runner._provider_routing
        reasoning_config = (policy.current_reasoning_config(runner, ctx.session_key) if policy else
            runner._resolve_session_reasoning_config(
                source=ctx.source, session_key=ctx.session_key, model=model, user_config=ctx.user_config,
                assignment=getattr(self, "_model_assignment", None)))
        from gateway.session_cli_turn import one_turn_reasoning
        reasoning_config = one_turn_reasoning(self, reasoning_config)
        runner._service_tier = (policy.current_service_tier(runner, ctx.session_key) if policy else
            runner._resolve_session_service_tier(source=ctx.source, session_key=ctx.session_key))
        from gateway.session_api_turn import api_execution
        api = api_execution.get()
        if api is not None:
            from gateway.platforms.api_server import _request_reasoning_config, _request_service_tier, _REQUEST_OPTION_MISSING
            requested_reasoning = _request_reasoning_config(api['settings'].get('model_options'))
            if requested_reasoning is not None:
                reasoning_config = requested_reasoning
            tier = _request_service_tier(api['settings'].get('model_options'))
            if tier is not _REQUEST_OPTION_MISSING:
                runner._service_tier = tier
        runner._reasoning_config = reasoning_config
        stream_consumer, stream_delta_cb, interim_cb, want_interim = self._setup_stream_consumer(platform_key)
        turn_route = runner._resolve_turn_agent_config(ctx.message, model, runtime_kwargs)
        agent, reused_cached_agent = self._resolve_turn_agent(
            turn_route, platform_key, combined_ephemeral, max_iterations, reasoning_config, pr,
        )
        if pending_fallback_notice:
            # Reuse the in-agent one-shot notice so the pre-agent provider switch is user-visible too.
            agent._pending_fallback_notice = pending_fallback_notice
        state = runner._session_state(ctx.session_key) if ctx.session_key else None
        attended_notice = getattr(getattr(state, "conversation", None), "pending_attended_notice", None)
        self._wire_turn_agent_callbacks(agent, turn_route, reasoning_config, stream_delta_cb, interim_cb, want_interim)
        from gateway.run_turn_progress import local_viewer_turn
        local_sinks = None
        if local_viewer_turn(self) and not ctx.mute_notification_reply:
            # Per-turn local-viewer callbacks must be installed on the shared cached agent only for
            # the duration of this run. Without this bridge the final response is painted twice by
            # the CLI/TUI path and billing/preview metadata never reaches the completion frame.
            from gateway import local_viewer_events
            local_sinks = local_viewer_events.wire(self, agent)
            from gateway.session_usage import TURN_BASE_ATTR
            setattr(agent, TURN_BASE_ATTR, int(getattr(agent, "session_output_tokens", 0) or 0))
        if attended_notice:
            self._emit_persistent_attended_notice(attended_notice)
            if state is not None:
                state.conversation.pending_attended_notice = None
        try:
            agent_history, observed_group_context, history_media_paths = self._load_turn_history(agent, reused_cached_agent)
            persist_msg, persist_ts = self._prepare_turn_message(agent_history)
            compressions = getattr(getattr(agent, "context_compressor", None), "compression_count", None)
            from gateway.session_cli_turn import run_classic_cli_turn
            from gateway.session_secret_prompt import secret_prompt_scope
            from gateway.session_sudo_prompt import sudo_prompt_scope
            with sudo_prompt_scope(self, agent), secret_prompt_scope(self, agent):
                result = run_classic_cli_turn(self, lambda: self._run_conversation_with_approval(
                    agent, agent_history, observed_group_context, persist_msg, persist_ts))
        finally:
            if local_sinks is not None:
                from gateway import local_viewer_events
                local_viewer_events.restore(agent, local_sinks)
                from gateway.session_usage import publish_usage
                publish_usage(self, agent, record=True)  # turn end: the final API call's counters
                from gateway.session_local_inventory import publish_settled_info
                publish_settled_info(self, agent)
        persisted_turn = _persisted_turn(agent, agent_history, result, compressions)
        from hermes_cli.goal_turn_outcome import model_final_response
        # Judge goals on the model's own text, before stream finish may add a recovery marker.
        model_response = model_final_response(result)
        # The hook returns the (possibly marked) result; hooks that only mutate in place return None.
        result = self._finish_stream_consumer(result, agent_history, stream_consumer) or result
        if _finite_viewer_turn() and getattr(agent, "_codex_session", None) is not None:
            # The classic `chat -q` process exit bounded the codex app-server child; a finite turn
            # hosted here must too, or every one-shot leaves one running until the idle-TTL sweep.
            agent._close_codex_session()
            agent._codex_session_prompt = None
        final_response = result.get("final_response")
        # Actual token counts from the agent instance used for this run.
        agent = ctx.agent_holder[0]
        has_comp = bool(agent) and hasattr(agent, "context_compressor")
        comp = agent.context_compressor if has_comp else None
        usage = {
            "last_prompt_tokens": getattr(comp, "last_prompt_tokens", 0) if has_comp else 0,
            "input_tokens": getattr(agent, "session_prompt_tokens", 0) if has_comp else 0,
            "output_tokens": getattr(agent, "session_completion_tokens", 0) if has_comp else 0,
            "model": getattr(agent, "model", None) if agent else None,
            # Most failed results name no provider; the error card's sign-in / update-key action needs it.
            "provider": getattr(agent, "provider", None) if agent else None,
            "context_length": (getattr(comp, "context_length", 0) or 0) if has_comp else 0,
            **{key: result.get(key) for key in _LEDGER_PASSTHROUGH_KEYS if key in result},
        }
        compacted_in_place, effective_session_id, history_offset = self._sync_session_after_run(agent_history)
        common = {
            "_model_final_response": model_response,
            "messages": result.get("messages", []), "api_calls": result.get("api_calls", 0),
            "failed": result.get("failed", False), "failure_reason": result.get("failure_reason"),
            # Usage-window limit (agent/plan_limits.py): the gateway offers the plan's model as a button.
            "usage_limit": result.get("usage_limit"),
            "partial": result.get("partial", False), "completed": result.get("completed"),
            "interrupted": result.get("interrupted", False), "interrupt_message": result.get("interrupt_message"),
            "error": result.get("error"),
            **{key: result[key] for key in _FAILURE_DESCRIPTOR_KEYS if key in result},
            "compression_exhausted": result.get("compression_exhausted", False),
            "compression_deferred": result.get("compression_deferred", False),
            "recovered_reply": result.get("recovered_reply", False),
            "acknowledged_by_reaction": result.get("acknowledged_by_reaction", False),
            "tools": ctx.tools_holder[0] or [],
            "history_offset": history_offset, "compacted_in_place": compacted_in_place, "session_id": effective_session_id,
            # The returned `messages` replaced the transcript, so a stored receipt keeps it whole.
            **({"_compressed": True} if compacted_in_place or effective_session_id != ctx.session_id else {}),
            # The loop's proven current-turn row: the stored receipt cuts there instead of re-guessing.
            **({"current_turn_user_idx": result["current_turn_user_idx"]} if "current_turn_user_idx" in result else {}),
            **usage,
            **({"persisted_turn": persisted_turn} if persisted_turn else {}),
        }
        previewed = result.get("response_previewed", False)
        viewer_meta = {}
        if local_sinks is not None:
            was_delivered = getattr(agent, "_interim_text_was_delivered", None)
            previewed = bool(previewed or (callable(was_delivered) and bool(final_response) and was_delivered(final_response) is True))
            viewer_meta = {
                "local_viewer": True,
                **({"billing_block": result["billing_block"]} if isinstance(result.get("billing_block"), dict) else {}),
                **({"warning": result["warning"]} if isinstance(result.get("warning"), str) and result["warning"] else {}),
            }
        if not final_response:
            # Keep the model copy raw for evaluation, but retain the finalized display copy here.
            # Some queued/direct delivery paths consume this result without the outer shape step.
            from gateway.run import _normalize_empty_agent_response, _sanitize_gateway_final_response
            display_response = _normalize_empty_agent_response(
                result, final_response or "", history_len=len(agent_history),
            )
            display_response = _sanitize_gateway_final_response(ctx.source.platform, display_response)
            if not display_response:
                display_response = t("gateway.shared.warn_passthrough", error=result["error"]) if result.get("error") else ""
            # Empty local-viewer turns still carry reasoning on the completion frame.
            # Omit agent_persisted: the caller defaults it differently when absent.
            if local_sinks is not None:
                common["last_reasoning"] = result.get("last_reasoning")
            return {"final_response": final_response or "", "delivery_text": display_response, **common, **viewer_meta,
                    "response_previewed": previewed,
                    "response_transformed": bool(result.get("response_transformed"))}
        final_response = self._append_auto_media_tags(final_response, result, agent_history, history_media_paths)
        return {
            "final_response": final_response, "delivery_text": final_response, "last_reasoning": result.get("last_reasoning"), **common, **viewer_meta,
            "response_previewed": previewed,
            "response_transformed": result.get("response_transformed", False),
            "response_reused": result.get("response_reused", False),
            # Lets the persistence block tell whether the codex app-server path self-persisted (it
            # didn't — see codex_runtime.py); default True keeps skip-db for the standard runtime.
            "agent_persisted": result.get("agent_persisted", True),
        }
