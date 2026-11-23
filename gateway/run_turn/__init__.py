"""Agent-turn execution for GatewayRunner: _handle_message_with_agent, _run_agent*, proxy path,
MCP reload. Bound onto ``GatewayRunner`` via the MRO; ``gateway.run`` internals
are imported lazily inside method bodies (import cycle) so ``patch("gateway.run.X")`` still works.
"""

from __future__ import annotations

from pm import install_hint
import logging
from typing import TYPE_CHECKING
import asyncio
import dataclasses
import inspect
import json
import os
import queue
import threading
import time
from agent.i18n import t
from agent.incognito import log_private_failure
from agent.notice_style import ContextNotice
from agent.error_classifier import is_context_overflow_reason
from agent.session_activity import AwakeIdleMeter, format_iteration_progress
from agent.turn_failure_copy import (
    FAILED_TURN_DISPLAY_KIND, FAILED_TURN_NOTICE, OPEN_TURN_TAIL_ROLES, PARTIAL_FAILED_TURN_NOTICE,
)
from contextlib import nullcontext, suppress
from contextvars import copy_context
from gateway.bot_peer_notices import automatic_notices_allowed
from gateway import loop_completion
from gateway.config import Platform
from gateway.cron_review import defer_review_followup
from gateway.media_repair import repair_explicit_computer_use_media_paths
from gateway.platforms.base import BasePlatformAdapter, ProcessingOutcome
from gateway.platforms.event import MessageEvent, turn_origin_for_event
from gateway.platforms.typing_state import call_typing_with_metadata
from gateway.session_context import bound_turn_origin, scoped_turn_actor, set_turn_origin
from gateway.run_turn_hygiene import hygiene_no_commit_reason
from gateway.run_turn_pending import _drain_leftover_steer
from gateway.run_loop_wake import GatewayLoopWakeMixin
from gateway.run_turn_persistence import GatewayTurnPersistenceMixin, _acp_launch
from gateway.platforms.helpers import hash_for_log, typing_metadata_for_turn
from gateway.response_filters import (
    display_kind_for_event, display_metadata_for_event, is_machinery_display_kind, persisted_user_metadata_for_event, reply_expected_metadata, silence_allowed,
    strip_edge_silence_markers,
)
from gateway.warning_notifications import diagnostic_metadata, diagnostic_turn_muted, diagnostic_wake_muted
from gateway.zero_cut_handover_drain import handover_runs_followups
from gateway.session import (
    SessionSource, _session_key_namespace, build_channel_continuity_note,
    build_session_context,
)
from gateway.run_turn_stream_delivery import stream_confirmed_final_delivery
from gateway.attended_model import (
    _stamp_seconds, format_attended_switch_back_notice, format_attended_switch_notice,
    is_active, parse_attended_config,
)
from gateway.session_transcript import TranscriptReadError
from gateway.run_turn_bubble_cleanup import settle_turn_senders
from gateway.run_turn_final_reconcile import edit_streamed_message, owns_reconcilable_preview
from gateway.turn_context import _GATEWAY_SILENCE_RETRY_NOTE, TurnContext, TurnWorker
from gateway.turn_lease import DEFAULT_LEASE_WAIT
from hermes_constants import get_hermes_home_override
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from utils import base_url_hostname

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.run import GatewayRunner
    from gateway.run_turn_runner import TurnRunner

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")

_tool_call_logger_lock = threading.Lock()
# Detached deletes of superseded turns' cards: a chat's flood wait never holds the turn's unwind.
_STALE_CLEANUP_TASKS: set = set()


async def _run_temporary_cleanups(cleanups) -> None:
    for cleanup in cleanups:
        try:
            result = cleanup()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.debug("Superseded turn's temporary cleanup failed", exc_info=True)
_NO_ATTENDED_ACTIVITY_STAMP = object()
# Bound on the hygiene gate's wait for a cooldown write scheduled by an unwinding or deferred attempt.
_attended_notice_lock = threading.RLock()


def _failed_turn_copy_is_user_facing(event, display_kind) -> bool:
    """Synthetic admissions must be retried/settled silently, never shown the resend boundary."""
    return getattr(event, "reply_expected", None) is not False and not is_machinery_display_kind(display_kind)


def _apply_failed_turn_response(response, event, display_kind, *, agent_failed_early,
                                is_context_overflow_failure, automatic_notice_allowed, notice):
    """Apply the failed-turn response contract after model execution and before persistence."""
    if not agent_failed_early or is_context_overflow_failure:
        return response
    if not _failed_turn_copy_is_user_facing(event, display_kind):
        return ""
    if automatic_notice_allowed:
        response = str(response or "").strip()
        return f"{response}\n\n{notice}" if response else notice
    return response


def _tool_call_logger() -> logging.Logger:
    """Process-wide ``hermes.tool_calls`` Logger + one RotatingFileHandler on logs/tool_calls.log.
    Named Loggers live in ``logging.Logger.manager.loggerDict`` forever, so the former per-turn name
    (``hermes.tool_calls.<id(log_queue)>``) leaked one Logger per logged turn (#62950); a single
    shared handler also keeps concurrent turns from double-writing lines."""
    tool_logger = logging.getLogger("hermes.tool_calls")
    with _tool_call_logger_lock:
        if not tool_logger.handlers:
            from logging.handlers import RotatingFileHandler
            from agent.redact import RedactingFormatter
            from gateway.run import _hermes_home

            log_dir = _hermes_home / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                log_dir / "tool_calls.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
            )
            handler.setFormatter(RedactingFormatter("%(message)s"))
            tool_logger.setLevel(logging.INFO)
            tool_logger.propagate = False
            tool_logger.addHandler(handler)
    return tool_logger


_CONTEXT_OVERFLOW_ERROR_PHRASES = (
    "context length", "context size", "context window",
    "maximum context", "token limit", "too many tokens",
    "reduce the length", "exceeds the limit",
    "request entity too large", "prompt is too long",
    "payload too large", "input is too long",
)

def _unexpected_silence_reply() -> str:
    """Reply when the model returned only a silence marker for a message that needed an answer."""
    return t("gateway.errors.unexpected_silence")


def is_context_overflow_failure_result(agent_result: dict, history_len: int) -> bool:
    """One verdict for "this failed turn is a context overflow", shared by transcript persistence
    (#1630 skip) and the user-facing reply so the two can never disagree.

    Multi-word phrases (not bare "exceed"/"token") avoid matching "rate limit exceeded" or
    "invalid authentication token"; a bare 400 only counts on a long session."""
    if not agent_result.get("failed"):
        return False
    # The classified reason wins over the historical text/length heuristic and any stale
    # compression flag. Only a legacy/unclassified result retains the #1630 growth-loop guard.
    err = str(agent_result.get("error") or "").lower()
    return is_context_overflow_reason(
        agent_result.get("failure_reason"),
        unclassified_overflow=bool(agent_result.get("compression_exhausted"))
        or any(p in err for p in _CONTEXT_OVERFLOW_ERROR_PHRASES)
        or ("400" in err and history_len > 50),
    )


# Setup/prefix rows rather than conversation: the agent rebuilds its own system prompt, and a
# transcript meta row is logging-only — neither reaches the model, but both are the head a
# fail-closed payload keeps.
_HYGIENE_SETUP_ROLES = ("system", "session_meta")


def bound_model_input_without_hygiene(history: list[Any], limit: int) -> list[Any]:
    """Fail-closed in-context bound for a turn where hygiene has not landed (#111988).

    Keeps the leading ``system``/``session_meta`` setup rows plus the newest tail, total <= ``limit``.
    Deterministic (the same transcript always yields the same cut) and payload-only: the stored
    transcript is never touched, so the agent's durable-prefix slice (``history_offset``) is
    unaffected. Returns ``history`` unchanged — same object — when nothing needs dropping, so the
    landed-compression and below-the-limit paths stay byte-identical.
    """
    if len(history) <= limit:
        return history
    head_end = 0
    while (head_end < len(history) and isinstance(history[head_end], dict)
           and history[head_end].get("role") in _HYGIENE_SETUP_ROLES):
        head_end += 1
    # Always leave room for the newest row: a setup-only payload would answer nothing.
    head_end = min(head_end, limit - 1)
    tail_start = len(history) - (limit - head_end)
    # Never start the kept tail on a tool result: its parent assistant(tool_calls) row is dropped
    # with it, and an orphaned tool result is an invalid sequence for every provider.
    while (tail_start < len(history) and isinstance(history[tail_start], dict)
           and history[tail_start].get("role") == "tool"):
        tail_start += 1
    return history[:head_end] + history[tail_start:]


from gateway.run_turn_prepare import GatewayTurnPrepareMixin
from gateway.run_background import GatewayBackgroundMixin
from gateway.run_turn_hygiene import GatewayTurnHygieneMixin


from gateway.run_turn.run_turn_attended import GatewayAttendedMixin
from gateway.run_turn.run_turn_execution import GatewayTurnExecutionMixin
from gateway.run_turn_mcp import GatewayTurnMCPMixin

class GatewayTurnMixin(GatewayTurnExecutionMixin, GatewayAttendedMixin, GatewayLoopWakeMixin, GatewayBackgroundMixin, GatewayTurnPrepareMixin, GatewayTurnHygieneMixin, GatewayTurnPersistenceMixin, GatewayTurnMCPMixin):
    """Agent-turn execution for GatewayRunner (see module docstring)."""




    def _snapshot_session_runtime_state(self, skey):
        override = self._session_model_override(skey) if skey else None
        state = self._peek_session_state(skey) if skey else None
        star = self._peek_session_state("*")
        last_model = ((state.conversation.last_resolved_model if state else "")
                      or (star.conversation.last_resolved_model if star else ""))
        epoch = self._session_model_override_epoch(skey) if skey else 0
        return dict(override) if override is not None else None, last_model, epoch

    def _commit_session_agent_runtime(self, skey, resolved):
        model, runtime, notice, cache_model = resolved
        # Worker resolution normally never writes session state; an unavailable attended route must
        # consume the staged switch-in line so the one fallback notice is the only notice for this turn.
        handoff = getattr(self, "_handoff_attended_state", None)
        if callable(handoff) and getattr(handoff, "__self__", None) is self:
            handoff(None, runner={"_pre_agent_fallback_notice": notice})
        else:
            # Test doubles and legacy bound runners do not expose the handoff helper; retain the
            # synchronous compatibility field for those callers.
            self._pre_agent_fallback_notice = notice
        if skey:
            fields = {"pre_agent_fallback_notice": notice}
            conversation = self._session_state(skey).conversation
            from gateway.run_turn_prepare import commit_stored_oauth_credentials
            commit_stored_oauth_credentials(conversation.model_override, runtime)
            if getattr(conversation, "model_pick_expired", False):
                from gateway.attended_model import friendly_model_name
                fields["pending_attended_notice"] = t("gateway.model.pick_expired", model=friendly_model_name(model))
                fields["model_pick_expired"] = False
            elif notice:
                fields["pending_attended_notice"] = None
            self._handoff_attended_state(skey, conversation=fields, wait=True)
            with _attended_notice_lock:
                notices = getattr(self, "_pre_agent_fallback_notices", None)
                if notices is None:
                    notices = self._pre_agent_fallback_notices = {}
                notices[skey] = notice
        if cache_model:
            self._handoff_attended_state(
                skey,
                conversation={"last_resolved_model": model} if skey else None,
                runner=None,
            )
            self._handoff_attended_state(
                "*", conversation={"last_resolved_model": model},
            )
        return model, runtime

    def _consume_pre_agent_fallback_notice(self, session_key: Optional[str]):
        """Consume the fallback notice for one session without crossing session boundaries."""
        if session_key:
            with _attended_notice_lock:
                notices = getattr(self, "_pre_agent_fallback_notices", None)
                if notices is not None and session_key in notices:
                    notice = notices.pop(session_key)
                    state = self._peek_session_state(session_key)
                    if state is not None:
                        state.conversation.pre_agent_fallback_notice = None
                    return notice
            state = self._peek_session_state(session_key)
            if state is not None:
                notice = state.conversation.pre_agent_fallback_notice
                state.conversation.pre_agent_fallback_notice = None
                return notice
        notice = getattr(self, "_pre_agent_fallback_notice", None)
        self._pre_agent_fallback_notice = None
        return notice

    @staticmethod
    def _load_session_agent_runtime(*, runner=None, source, skey, user_config, config, override, last_model, attended_route=None):
        """Resolve a detached snapshot off-loop; all runner-state commits belong to the caller."""
        from gateway.run_turn_prepare import load_session_agent_runtime
        return load_session_agent_runtime(runner=runner, source=source, skey=skey, user_config=user_config, config=config,
                                          override=override, last_model=last_model, attended_route=attended_route)

    async def _resolve_session_agent_runtime_async(
        self, *, source: Optional[SessionSource] = None, session_key: Optional[str] = None,
        user_config: Optional[dict] = None,
    ) -> tuple[str, dict]:
        """Off-loop reads/credentials, on-loop snapshots and compare-and-set commits."""
        # Slash commands such as manual /compress resolve a session without an inbound turn
        # config snapshot. Role holds are still live persisted routes, so normalize that
        # omission through the same gateway loader used by ordinary turns before resolving
        # aliases. An empty config would make a valid stored alias look unavailable.
        if user_config is None:
            from gateway.run import _load_gateway_config
            user_config = _load_gateway_config()
        from gateway.session_policy import policy_for_source
        policy = policy_for_source(self, source) if source is not None else None
        if policy is not None:
            # A launch-policy session runs its frozen route; resolution may read credentials.
            return await asyncio.to_thread(
                self._resolve_session_agent_runtime, source=source, session_key=session_key, user_config=user_config)
        skey = self._resolve_session_key_or_none(source, session_key)
        self._pre_agent_fallback_notice = None
        activity_stamp = _NO_ATTENDED_ACTIVITY_STAMP
        attended_snapshot = {}
        attended_snapshot_epoch = None
        if skey:
            await self._rehydrate_session_model_selection_async(skey)
            state = self._session_state(skey)
            attended_enabled = parse_attended_config(user_config or {}).enabled
            if attended_enabled:
                state.conversation.pre_agent_fallback_notice = None
            await self._rehydrate_session_model_override_async(skey)
            if not attended_enabled:
                await self._expire_attended_temporary_picks_async(skey, user_config)
            # A role hold resolves through the same route decision with attended routing off, so it
            # takes the same off-loop snapshot rather than reading the session store on the loop.
            if attended_enabled or state.conversation.model_role:
                current_override = self._session_model_override(skey) or {}
                attended_cfg = parse_attended_config(user_config or {})
                attended_snapshot_epoch = self._session_model_override_epoch(skey)
                attended_snapshot = await asyncio.to_thread(
                    self._load_attended_resolution_snapshot,
                    skey, str(current_override.get("model") or ""), attended_cfg.model, user_config,
                )
                activity_stamp = attended_snapshot.get("activity_stamp")
                if attended_enabled:
                    if state.conversation.last_user_activity_at is None:
                        state.conversation.last_user_activity_at = activity_stamp
                    await self._expire_attended_temporary_picks_async(
                        skey, user_config, activity_stamp=activity_stamp,
                    )
                if state.conversation.reasoning_override is None:
                    attended_snapshot["reasoning_override"] = None
                    attended_snapshot["reasoning_scope"] = "session"
        while True:
            override, last_model, epoch = self._snapshot_session_runtime_state(skey)
            if attended_snapshot and epoch != attended_snapshot_epoch:
                # /attend can race credential loading just like /model. Retry with a fresh
                # off-loop snapshot rather than committing the pin from the prior epoch.
                attended_snapshot_epoch = epoch
                attended_snapshot = await asyncio.to_thread(
                    self._load_attended_resolution_snapshot,
                    skey, str((override or {}).get("model") or ""), attended_cfg.model, user_config,
                )
                continue
            attended_route = self._attended_route_for_session(
                source=source, session_key=skey, user_config=user_config, override=override,
                last_model=last_model, activity_stamp=activity_stamp,
                new_session=attended_snapshot.get("new_session", False) if attended_snapshot else False,
                reasoning_override=(attended_snapshot.get("reasoning_override", _NO_ATTENDED_ACTIVITY_STAMP)
                                    if attended_snapshot else _NO_ATTENDED_ACTIVITY_STAMP),
                reasoning_scope=attended_snapshot.get("reasoning_scope") if attended_snapshot else None,
                default_reasoning=attended_snapshot.get("default_reasoning") if attended_snapshot else None,
                attended_reasoning=attended_snapshot.get("attended_reasoning") if attended_snapshot else None,
                attended_pinned=attended_snapshot.get("attended_pinned", False),
                attended_pin_until=attended_snapshot.get("attended_pin_until") if attended_snapshot else None,
                durable_activity_stamp=attended_snapshot.get(
                    "durable_activity_stamp", _NO_ATTENDED_ACTIVITY_STAMP),
                route_active=attended_snapshot.get("route_active", _NO_ATTENDED_ACTIVITY_STAMP),
            )
            from gateway.run_turn_prepare import load_runtime_for_source
            resolved = await asyncio.to_thread(
                load_runtime_for_source, self, source=source, skey=skey, user_config=user_config,
                override=override, last_model=last_model, attended_route=attended_route,
            )
            # Credential resolution can also yield to /model or a boundary. Retry
            # against live memory, not disk, so a pending durable clear cannot revive it.
            if not skey or self._session_model_override_epoch(skey) == epoch:
                return self._commit_session_agent_runtime(skey, resolved)





    @staticmethod
    def _pop_post_delivery_callback(adapter, key, generation):
        """Pop the adapter's deferred post-delivery callback for ``key`` (legacy dict fallback)."""
        if getattr(type(adapter), "pop_post_delivery_callback", None) is not None:
            return adapter.pop_post_delivery_callback(key, generation=generation)
        if adapter and hasattr(adapter, "_post_delivery_callbacks"):
            return adapter._post_delivery_callbacks.pop(key, None)
        return None





    async def _wake_pin_matches(self, event, pinned: str, session_id: str) -> bool:
        """Keep strict routing pinned, except for a verified, still-current compressed wake."""
        if pinned == session_id:
            return True
        if not GatewayTurnMixin._is_gateway_wake(event):
            return False
        from hermes_cli.session_controls import internal_wake_is_current
        if not await asyncio.to_thread(internal_wake_is_current, event, session_id):
            return False
        return event.metadata.get("gateway_session_id") == session_id

    async def _strict_session_pin_holds(self, event, session_id: Optional[str] = None) -> bool:
        """The ``_hmwa_resolve_session`` strict rule for every other admission and queue boundary.

        True for an unpinned event. A ``gateway_session_strict`` event holds only while its pinned
        ``gateway_session_id`` still owns ``gateway_session_key`` and, when ``session_id`` is given,
        is the session about to run it: after /new or /resume rebinds the key the event is dropped,
        never run in the conversation that replaced it."""
        metadata = getattr(event, "metadata", None)
        if not isinstance(metadata, dict) or not metadata.get("gateway_session_strict"):
            return True
        pinned = str(metadata.get("gateway_session_id") or "").strip()
        key = str(metadata.get("gateway_session_key") or "").strip()
        current = None
        if pinned and key:
            entry = await self.async_session_store.lookup_by_session_key(key)
            current = getattr(entry, "session_id", None)
        if current is not None and (session_id is None or session_id == current):
            if await GatewayTurnMixin._wake_pin_matches(self, event, pinned, current):
                return True
        logger.warning(
            "Dropping internally routed event: pinned session id=%s no longer owns key=%s (running=%s)",
            pinned or "missing", key or "missing", session_id or "-",
        )
        return False

    @staticmethod
    def _is_gateway_wake(event) -> bool:
        """A goal continuation or a loop tick, recognized by its stamp, never by ``internal``."""
        from hermes_cli.session_controls import is_gateway_wake
        return is_gateway_wake(event)

    async def _internal_wake_input_at_execution(self, event, session_id: str, message: str) -> Optional[str]:
        """Re-check a gateway wake at the model boundary; return the message to run, or None to drop.

        Admission and session resolution already fenced the wake, but turn preparation and hooks
        await before the agent starts, and a pause, stop, clear or replacement can commit in that
        window. A goal continuation whose definition was edited since gets its refreshed prompt."""
        from gateway.platforms.event import is_goal_continuation_event
        from hermes_cli.session_controls import loop_wake_is_current, refresh_goal_continuation
        metadata = getattr(event, "metadata", None) or {}
        if metadata.get("gateway_session_strict"):
            pin_check = getattr(self, "_strict_session_pin_holds", None)
            if callable(pin_check):
                if not await pin_check(event, session_id):
                    return None
            else:
                lookup = getattr(getattr(self, "async_session_store", None), "lookup_by_session_key", None)
                if callable(lookup):
                    current = await lookup(metadata.get("gateway_session_key", ""))
                    if (getattr(current, "session_id", None) != session_id
                            or getattr(current, "session_id", None) != metadata.get("gateway_session_id")):
                        from hermes_cli.session_controls import internal_wake_is_current
                        if not internal_wake_is_current(event, session_id):
                            return None
        if is_goal_continuation_event(event):
            before = event.text
            refreshed = await self._run_in_executor_with_context(refresh_goal_continuation, event, session_id)
            if refreshed is None:
                return None
            if refreshed != before:
                message = message.replace(before, refreshed) if before and before in message else refreshed
        current = await self._run_in_executor_with_context(loop_wake_is_current, metadata, session_id)
        if metadata.get("loop_wakeup_session_id"):
            from hermes_cli.session_controls import migrated_successor
            successor = await self._run_in_executor_with_context(
                migrated_successor, "loop", metadata.get("loop_wakeup_session_id"),
            )
            if successor and successor != session_id:
                current = await self._run_in_executor_with_context(
                    loop_wake_is_current, metadata, successor,
                )
        if not current:
            return None
        from hermes_cli.session_controls import retry_wake_is_current
        if not await self._run_in_executor_with_context(retry_wake_is_current, metadata):
            # A queued retry notice whose retry was cancelled (/stop, delegate_task stop) or
            # superseded while it waited behind the running turn.
            return None
        fence = getattr(event, "_gateway_loop_execution_fence", None)
        rebound_sid = metadata.get("loop_wakeup_session_id")
        if fence is not None and rebound_sid and fence[0] != rebound_sid:
            from hermes_cli.loops import loop_control_generation
            event._gateway_loop_execution_fence = (
                rebound_sid, fence[1], fence[2], loop_control_generation(rebound_sid),
            )
        return message































































    # reasoning_style → (header catalog key, per-line quote prefix for blank / non-blank lines)
    _REASONING_QUOTE_STYLES = {
        "subtext": ("gateway.reasoning.quote_label_discord", "-# ", "-#"),
        "blockquote": ("gateway.reasoning.quote_label_md", "> ", ">"),
    }







    # One owner for the boundary copy (agent/turn_failure_copy.py): the core closer in
    # agent/conversation_loop.py writes the same row on the paths that never reach this layer.















    async def _hmwa_persist_proxy_recovery_user_row(
        self, *, session_id: str, message: str, inbound_message_id: Optional[str],
        recovery_id: Optional[str], recovery_session_key: Optional[str],
        persist_user_message: Optional[Any], persist_user_timestamp: Optional[float],
        persist_user_display_kind: Optional[str], persist_user_display_metadata: Optional[dict],
    ) -> bool:
        """Persist a recovered proxy turn's user row before the remote model can run.

        The local gateway owns restart durability even when execution is delegated. The marker on
        the result lets the normal gateway closeout append only the assistant side of the proxy
        response, avoiding a second user row.
        """
        if recovery_id is None and recovery_session_key is None:
            return False
        store = self.async_session_store
        # A legacy id-less recovery has no dedupe key of its own; never stamp the session key as
        # one (it would collide across recoveries). Restart recovery uses its content fallback.
        platform_message_id = inbound_message_id or recovery_id
        row = {
            "role": "user",
            "content": persist_user_message if persist_user_message is not None else message,
            "timestamp": persist_user_timestamp if persist_user_timestamp is not None else time.time(),
        }
        if platform_message_id is not None:
            row["platform_message_id"] = str(platform_message_id)
        if persist_user_display_kind:
            row["display_kind"] = persist_user_display_kind
        if isinstance(persist_user_display_metadata, dict):
            row["display_metadata"] = dict(persist_user_display_metadata)
        if platform_message_id is not None:
            probe = getattr(store, "has_platform_message_id", None)
            if callable(probe):
                already_present = probe(session_id, str(platform_message_id))
                if inspect.isawaitable(already_present):
                    already_present = await already_present
                if already_present:
                    return True
        await store.append_to_transcript(session_id, row)
        return True

    async def _hmwa_store_turn_prompt_tokens(
        self, store, entry_key, session_key, last_prompt_tokens, turn_epoch, *, touch_activity,
    ):
        """Store this turn's prompt-token count for hygiene, unless a deferred hygiene summary was
        adopted while the turn ran: that count measured the pre-compaction transcript and would
        re-trigger hygiene on the compacted one, so 0 (fall back to the estimate) is stored instead.

        The write runs off-loop, so an adoption can land between the epoch check and the write, and
        its own off-loop ``last_prompt_tokens=0`` may finish first and be overwritten by this stale
        count. The epoch is bumped on the loop before the adoption schedules its write, so a second
        check after this write lands closes the window: unchanged means any later adoption writes
        its 0 after ours; changed means re-zero here."""
        if self._hygiene_deferred_adoption_epoch(session_key) != turn_epoch:
            last_prompt_tokens = 0
        await store.update_session(entry_key, last_prompt_tokens=last_prompt_tokens, touch_activity=touch_activity)
        if last_prompt_tokens and self._hygiene_deferred_adoption_epoch(session_key) != turn_epoch:
            await store.update_session(entry_key, last_prompt_tokens=0, touch_activity=False)





    # Chat-side next steps keyed by HTTP status; Hermes commands only (/login is the gateway's own
    # sign-in, `{relogin}` the profile-aware host equivalent, filled from the turn's agent provider).
    # Values are catalog keys (``gateway.errors.hint_*``); 401 carries a ``{relogin}`` placeholder.
    _STATUS_HINTS = {
        401: "gateway.errors.hint_auth",
        402: "gateway.errors.hint_quota",
        529: "gateway.errors.hint_overloaded",
    }



    def _hmwa_discard_stale_result(self, source, _quick_key, run_generation):
        """A newer run generation superseded this turn: drop its deferred post-delivery callback,
        except the part that deletes its temporary progress cards, which still runs."""
        logger.info(
            "Discarding stale agent result for %s — generation %d is no longer current",
            _quick_key or "?", run_generation,
        )
        callback = self._pop_post_delivery_callback(self._delivery_adapter_for(source), _quick_key, run_generation)
        from gateway.platforms.base import temporary_cleanups
        cleanups = temporary_cleanups(callback)
        if cleanups:
            task = asyncio.get_running_loop().create_task(_run_temporary_cleanups(cleanups))
            _STALE_CLEANUP_TASKS.add(task)
            task.add_done_callback(_STALE_CLEANUP_TASKS.discard)





    async def _handle_message_with_agent(self, event, source, _quick_key: str, run_generation: int):  # health: allow CC -- session wake execution composes strict pins, leases, and final loop fences.
        """Inner handler that runs under the _running_agents sentinel guard."""
        # The outbound string (or streamed None) loses the raw result's outcome.
        # Fail closed through preparation, execution and delivery exceptions.
        event._agent_turn_succeeded = False
        _msg_start_time = time.time()
        _platform_name = source.platform.value if hasattr(source.platform, "value") else str(source.platform)
        logger.info(
            "inbound message: platform=%s user=%s chat=%s msg_len=%d reply_to_id=%s",
            _platform_name, hash_for_log(source.user_id), hash_for_log(source.chat_id),
            len(event.text or ""), getattr(event, "reply_to_message_id", None),
        )

        if not await self._prepare_loop_wake_execution(event, source):
            self._log_loop_wake_suppressed(event, "superseded before session resolution")
            return
        _owned_loop_fence = getattr(event, "_gateway_loop_execution_fence", None)
        if _owned_loop_fence is not None:
            run_generation = _owned_loop_fence[2]

        from gateway.run_turn_setup_guard import run_preagent_setup
        setup, reply = await run_preagent_setup(self, event, source, _quick_key, run_generation)
        if setup is None:
            return reply
        source, session_entry, session_key, prepared, _session_env_tokens, hook_ctx, channel = setup
        if hook_ctx is None:  # a heartbeat whose owner moved during setup: nothing to run
            self._clear_session_env(_session_env_tokens)
            return None
        history, message_text = prepared.history, prepared.message_text
        from gateway.platforms.event import GOAL_CONTINUATION_METADATA_KEY
        if (
            isinstance(getattr(event, "metadata", None), dict)
            and event.metadata.get(GOAL_CONTINUATION_METADATA_KEY)
            and event.reply_expected is not True
        ):
            # The structured marker survives adapter copies/recovery; derive the quiet contract at
            # the turn boundary instead of trusting a rebuilt event's nullable field.
            event.reply_expected = False
        # Snapshot the interruption marker before preparation/delivery can yield to a successor turn.
        _resume_pending_marker = None
        _resume_marker_reader = None
        try:
            # Lightweight runners (tests, plugins) may have no session store at all; the
            # ``async_session_store`` property itself raises AttributeError then.
            _resume_marker_reader = getattr(self.async_session_store, "get_resume_pending_marker", None)
        except AttributeError:
            pass
        _resume_marker_reader_available = callable(_resume_marker_reader)
        if _resume_marker_reader_available:
            try:
                _resume_pending_marker = _resume_marker_reader(session_key)
                if inspect.isawaitable(_resume_pending_marker):
                    _resume_pending_marker = await _resume_pending_marker
            except Exception as _e:
                # Lightweight runners and plugins may expose only the older store surface. Keep
                # their historical unconditional clear rather than making marker lookup required.
                _resume_marker_reader_available = False
                logger.debug("resume marker snapshot unavailable for %s: %s", session_key, _e)
        event._restart_note_marker_api_available = _resume_marker_reader_available
        event._restart_note_expected_marker = _resume_pending_marker if _resume_marker_reader_available else None
        try:
            # run_preagent_setup already ran history/session preparation (including hygiene).
            if not await self._prepare_loop_wake_execution(event, source):
                self._log_loop_wake_suppressed(event, "superseded during turn preparation")
                return
            from gateway.run_checkins import prepare_checkin
            if not await prepare_checkin(self, event, session_entry):
                return
            if not await self._prepare_loop_wake_execution(event, source):
                self._log_loop_wake_suppressed(event, "superseded during turn preparation")
                return
            # The setup guard already emitted agent:start and checked heartbeat ownership.
            # Capture the launch session id so post-run compression publication is identity-guarded
            # (a /new may move session_entry.session_id while the old run is still unwinding).
            if self._is_user_turn_event(event):
                # The user is back: release completions held since their /stop.
                self._clear_user_stop_latch(session_key)
                self._clear_user_stop_latch(_quick_key)
            await self._revive_blocked_goal_for_user_turn(session_entry, source, event)
            _run_start_session_id = session_entry.session_id
            _loop_tick = self._loop_wake_command_identity(event)
            _loop_tick = _loop_tick[1] if _loop_tick is not None else None
            if not await self._prepare_loop_wake_execution(event, source):
                self._log_loop_wake_suppressed(event, "superseded during agent:start hooks")
                return
            if _loop_tick is not None:
                event._gateway_loop_tick = _loop_tick
            _turn_started_monotonic = time.monotonic()
            # Admission/typing is not execution. All routing, authorization and
            # turn preparation gates have passed when the agent runner is entered.
            event._heartbeat_execution_started = True
            _turn_channel_prompt, _turn_source = channel
            # The setup guard persists coherent prompt pins before agent:start; no second write here.
            # The last fence before the model, for every gateway wake whatever its internal flag
            # (goal continuations are built with internal=False): a control committed during
            # preparation or the start hooks must not let the obsolete wake run.
            if GatewayTurnMixin._is_gateway_wake(event):
                message_text = await GatewayTurnMixin._internal_wake_input_at_execution(
                    self, event, _run_start_session_id, message_text)
                if message_text is None:
                    logger.info("Discarding wake superseded during turn preparation for %s", session_key)
                    return None
            if not await prepare_checkin(self, event, session_entry, start=True):
                return
            if (event.metadata or {}).get("checkin"):
                message_text = event.text
            event._loop_completion_identity = await self._run_in_executor_with_context(
                loop_completion.capture_loop_identity, _run_start_session_id)
            started, agent_result = await self._invoke_loop_wake_effect(
                event, self._run_agent,
                message=message_text, context_prompt=prepared.context_prompt, history=history, source=_turn_source,
                session_id=_run_start_session_id, session_key=session_key,
                run_generation=run_generation, event_message_id=self._reply_anchor_for_event(event),
                inbound_message_id=str(event.message_id) if event.message_id else None,
                recovery_id=getattr(event, "_hermes_recovery_id", None),
                recovery_session_key=getattr(event, "_hermes_recovery_session_key", None),
                channel_prompt=_turn_channel_prompt, internal=event.internal, event_metadata=dict(event.metadata or {}),
                moa_config=getattr(event, "_moa_config", None),
                title_user_message=prepared.title_user_message,
                persist_user_message=prepared.persist_user_message,
                persist_user_timestamp=prepared.persist_user_timestamp,
                persist_user_display_kind=prepared.persist_user_display_kind,
                reply_expected=event.reply_expected,
                persist_user_display_metadata={
                    "gateway_input_owner": prepared.persistence_owner, **persisted_user_metadata_for_event(event),
                    **reply_expected_metadata(event.reply_expected), **diagnostic_metadata(event)},
                message_type=event.message_type,
                scheduled_heartbeat=bool(getattr(event, "_heartbeat_session_id", None)),
            )
            if not started:
                self._log_loop_wake_suppressed(event, "execution fence rejected the model run")
                return
            from gateway.session_automation_retry import held_runtime_refusal
            if held_runtime_refusal(event, agent_result):
                return None  # The FIFO retries this unexecuted input without a transcript apology.
            _turn_seconds = time.monotonic() - _turn_started_monotonic

            # A queued (/queue) chain answered the LAST message of the chain, so the outer final
            # send (bracketed by the adapter against this event) must be ledgered under that
            # message's id or it collides with an earlier turn's row carrying the same text. Reply
            # routing is untouched: the anchor still comes from this event.
            if isinstance(agent_result, dict):
                if "queued_terminal_reply_receipt" in agent_result:
                    event._terminal_reply_latency_receipt = agent_result["queued_terminal_reply_receipt"]
                _terminal_inbound = agent_result.get("queued_terminal_inbound_id")
                if _terminal_inbound:
                    event.ledger_message_id = str(_terminal_inbound)
                if "queued_terminal_notification_category" in agent_result:
                    event.metadata["notification_category"] = agent_result["queued_terminal_notification_category"]
                if agent_result.get("queued_terminal_notification_origin"):
                    # A completion drained behind this turn is fresh goal evidence for the
                    # chain's single post-turn judge (see _run_post_turn_hooks).
                    event.metadata["notification_origin"] = agent_result["queued_terminal_notification_origin"]
                if isinstance(agent_result.get("_notification_reply_muted"), bool):
                    event._notification_reply_muted = agent_result["_notification_reply_muted"]
                if isinstance(agent_result.get("queued_terminal_goal_identity"), dict):
                    # Post-turn goal hooks judge the terminal turn: carry its identity on the head.
                    event._post_turn_goal_identity = agent_result["queued_terminal_goal_identity"]
                loop_completion.carry_response(agent_result, event)

            await self._hmwa_stop_typing_for_turn(event, source)

            if not self._is_session_run_current(_quick_key, run_generation):
                self._hmwa_discard_stale_result(source, _quick_key, run_generation)
                return None

            response, _intentional_silence, agent_messages = await self._hmwa_shape_agent_response(
                agent_result, source, history, session_entry, session_key,
                _quick_key, run_generation, _run_start_session_id, _platform_name, _msg_start_time,
                persist_user_display_kind=prepared.persist_user_display_kind,
                reply_expected=event.reply_expected,
                loop_identity=getattr(event, "_loop_completion_identity", None),
            )
            if _intentional_silence:
                self._hmwa_pause_typing_after_silence(event, source)
            response = self._usage_window_chat_wording(agent_result, self._hmwa_prepend_reasoning(agent_result, response, source, _intentional_silence))
            from gateway.response_filters import strip_trailing_loop_complete_marker
            response = strip_trailing_loop_complete_marker(response)
            _footer_line = self._hmwa_runtime_footer_line(agent_result, source, _turn_seconds)
            from gateway.runtime_footer import FooteredReply  # streamed: the footer goes out as a trailing send instead
            if _footer_line and response and not agent_result.get("already_sent") and not _intentional_silence:
                response = FooteredReply(response, _footer_line)  # its send may put it on a photo caption (#74547)
            _terminal_display_kind = agent_result.get("queued_terminal_display_kind", prepared.persist_user_display_kind)
            if _terminal_display_kind != "supervisor_checkin":
                await self._hmwa_post_turn_hooks(hook_ctx, agent_result, response)

            agent_failed_early, hidden_reasoning_incomplete, is_context_overflow_failure = (
                self._hmwa_classify_turn_failure(agent_result, history, session_entry)
            )
            response = _apply_failed_turn_response(
                response, event, prepared.persist_user_display_kind,
                agent_failed_early=agent_failed_early,
                is_context_overflow_failure=is_context_overflow_failure,
                automatic_notice_allowed=automatic_notices_allowed(source),
                notice=self._hmwa_failed_turn_notice(agent_result),
            )
            response, session_entry = await self._hmwa_compression_exhaustion_reset(
                agent_result, response, session_entry, session_key, source, event=event,
            )
            # Normalize supervisor output after generic error/empty-reply shaping, so a provider
            # explainer cannot replace it in either delivery or the captured completion.
            from gateway.run_checkins import silence_fallback
            checkin_result = await silence_fallback(self, event, session_entry, agent_result)
            if checkin_result is not agent_result:
                agent_result = checkin_result
                response = agent_result["final_response"]
                _intentional_silence = agent_result["intentional_silence"]
            if _terminal_display_kind == "supervisor_checkin":
                await self._hmwa_post_turn_hooks(hook_ctx, agent_result, response)
            await self._hmwa_persist_turn_transcript(
                event=event, source=source, session_entry=session_entry, session_key=session_key,
                agent_result=agent_result, agent_messages=agent_messages, prepared=prepared,
                response=response, agent_failed_early=agent_failed_early,
                hidden_reasoning_incomplete=hidden_reasoning_incomplete,
                is_context_overflow_failure=is_context_overflow_failure,
            )
            await self._record_room_silence_decision(
                session_key, agent_result, _intentional_silence,
                display_kind=prepared.persist_user_display_kind, reply_expected=event.reply_expected)
            delivered_response = await self._deliver_turn_response_then_limit_offer(
                event, source, session_entry, session_key, run_generation,
                agent_result, agent_messages, response, _footer_line, _intentional_silence,
            )
            from gateway.run import _should_clear_resume_pending_after_turn
            event._agent_turn_succeeded = _should_clear_resume_pending_after_turn(agent_result)
            if event._agent_turn_succeeded:
                try:
                    if _resume_marker_reader_available:
                        _clear_kwargs = {"expected_marker": _resume_pending_marker}
                        _own_turn_token = getattr(event, "_gateway_active_turn_token", None)
                        if _own_turn_token is not None:
                            _clear_kwargs["expected_turn_id"] = _own_turn_token
                        await self.async_session_store.clear_resume_pending(session_key, **_clear_kwargs)
                    else:
                        await self.async_session_store.clear_resume_pending(session_key)
                except Exception as _e:
                    logger.debug("clear_resume_pending after delivery failed for %s: %s", session_key, _e)
            return delivered_response

        except Exception as e:
            return await self._hmwa_agent_error_reply(e, event, source, session_entry, session_key, prepared)
        finally:
            try:
                from gateway.run_checkins import reconcile_turn
                await reconcile_turn(self, source, session_key)
            except Exception:
                logger.warning("Could not reconcile supervisor check-in", exc_info=True)
            # Restore session context variables to their pre-handler state
            self._clear_session_env(_session_env_tokens)

    def _profile_scope_for_source(self, source: SessionSource):
        """``_profile_runtime_scope`` for ``source``'s profile when a secret scope is required.

        Under multiplexing config/skills/memory resolve to the source profile's home AND credentials
        come from its secret scope (never process-global ``os.environ``). A standalone gateway
        (``multiplex_profiles`` off) still binds once a hosted room has flipped the process-wide
        credential guard — see ``_standalone_launch_scope``."""
        from gateway.run import _profile_runtime_scope
        from gateway.session_policy import local_desktop_turn
        home = self._profile_scope_key_for_source(source)
        return local_desktop_turn(self, source, _profile_runtime_scope(home) if home is not None
                                  else self._standalone_launch_scope())

    def _profile_scope_key_for_source(self, source: SessionSource) -> Optional[Path]:
        """Profile home ``_profile_scope_for_source`` binds for ``source``, or ``None`` when it falls
        back to the standalone launch scope. The single owner of that branch condition: callers that
        group work per scope (heartbeat restore) key on this so they cannot drift from the scope
        actually entered."""
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            return self._resolve_profile_home_for_source(source)
        return None

    def _async_profile_scope_for_source(self, source: SessionSource):
        """``async with`` twin of :meth:`_profile_scope_for_source` (secret hydration off-loop).

        Slash dispatch runs under the RECEIVING bot's scope (auth needs its ``.env``), which is not
        the routed runtime when a bot serves another profile's chat; every handler reading
        home-relative state (pending writes, memory store, config) binds the runtime here (#119915)."""
        from gateway.run import _async_profile_runtime_scope
        home = self._profile_scope_key_for_source(source)
        if home is not None:
            return _async_profile_runtime_scope(home)
        from tui_gateway.launch_profile_policy import async_launch_profile_scope_if_multiplexed
        return async_launch_profile_scope_if_multiplexed()

    @staticmethod
    def _standalone_launch_scope():
        """Scope for a standalone gateway's own (launch-profile) work: a no-op until the process hosts
        another profile home, then the launch profile's OWN runtime scope.

        A native hosted room running a second profile calls
        ``tui_gateway.launch_profile_policy.activate_multi_profile_hosting`` inside the gateway process,
        so ``get_secret`` fails closed for every unscoped read afterwards — including the standalone
        gateway's ordinary turns, which never bound a scope because ``multiplex_profiles`` is off
        (#112878). The launch profile is a profile too: bind its ``.env`` over the env frozen at
        activation (a key injected by systemd / ``op run`` has no file to rebuild it from), never a
        secondary's scope and never live ``os.environ``."""
        from tui_gateway.launch_profile_policy import launch_profile_scope_if_multiplexed
        return launch_profile_scope_if_multiplexed()

    def _media_delivery_scope_for_source(self, source: SessionSource):
        """Home + terminal-policy scope for validating a turn's MEDIA / local-file paths on the
        adapter's delivery side, which runs after the routed turn scope was reset.

        Docker path translation (``platforms/base.py::_translate_docker_container_media_path``)
        infers the producing container from the ACTIVE profile (``get_active_profile_name``) and the
        scope-aware ``TERMINAL_DOCKER_VOLUMES``; without this a secondary's ``MEDIA:/output/x.png``
        resolves against the default profile's sandbox and mounts (#109024). No secret hydration:
        path validation reads no credentials and this runs on the event loop."""
        if not getattr(getattr(self, "config", None), "multiplex_profiles", False):
            return nullcontext()
        from gateway.run import _profile_runtime_scope
        return _profile_runtime_scope(self._resolve_profile_home_for_source(source), {})

    def _reset_notice_session_info(self, source: SessionSource) -> str:
        """Session-info block for the auto-reset notice, resolved inside the profile serving ``source``.

        Call via ``asyncio.to_thread``: resolution can block (credential refresh, context-length
        probes), and the scope is entered here so contextvars behave in the worker thread."""
        with self._profile_scope_for_source(source):
            return self._format_session_info(source)

    def _format_session_info(self, source: Optional[SessionSource] = None) -> str:
        """Model / provider / context-length / endpoint block so users can spot bad context detection.
        With ``source``, the chat's ``channel_overrides`` route is reported (what its turn runs)."""
        from gateway.run import _resolve_gateway_model_context
        from gateway.session_info_route import channel_override_route
        model, route, force_provider = channel_override_route(source and self._serving_channel_config(source), source)
        resolved = _resolve_gateway_model_context(model=model, route=route)
        if force_provider:
            resolved = dataclasses.replace(resolved, provider=force_provider)
        context_length = resolved.context_length
        ctx_source = {
            "config": "config",
            "default": "fallback, not a detected limit — set model.context_length in config to override",
        }.get(resolved.context_source, "detected")
        ctx_display = (
            f"{context_length / 1_000_000:.1f}M" if context_length >= 1_000_000
            else f"{context_length // 1_000}K" if context_length >= 1_000 else str(context_length)
        )
        lines = [
            t("gateway.session.info_model", model=resolved.model),
            t("gateway.session.info_provider", provider=resolved.provider or "openrouter"),
            t("gateway.session.info_context", tokens=ctx_display, source=ctx_source),
        ]
        if (resolved.provider or "") == "moa":
            # The preset name hides who pays: the aggregator runs every tool-loop step (#112359).
            from hermes_cli.config import load_config
            from hermes_cli.moa_config import normalize_moa_config
            agg = normalize_moa_config(load_config().get("moa"))["presets"].get(resolved.model, {}).get("aggregator") or {}
            if agg:
                lines.append(t("gateway.session.info_acting_model", provider=agg.get("provider"), model=agg.get("model")))
        base_url = resolved.base_url
        if base_url and base_url_hostname(base_url) in ("localhost", "127.0.0.1", "0.0.0.0"):
            lines.append(t("gateway.session.info_endpoint", url=base_url))
        return "\n".join(lines)

    def _resolve_enabled_toolsets_for_source(
        self, user_config: dict, source: SessionSource, platform_key: str,
    ) -> list:
        """Enabled toolsets for an agent run, honoring an adapter ``toolsets_for_source()`` override
        validated through the SAME ``_get_platform_tools`` path (unknown / platform-restricted
        toolsets dropped, not trusted)."""
        from hermes_cli.tools_config import _get_platform_tools
        try:
            adapter = self._delivery_adapter_for(source)
            override = adapter.toolsets_for_source(source) if adapter is not None else None
        except Exception:
            override = None
        if isinstance(override, list):
            pts = dict(user_config.get("platform_toolsets") or {})
            pts[platform_key] = [str(x) for x in override]
            user_config = {**user_config, "platform_toolsets": pts}
        return sorted(_get_platform_tools(user_config, platform_key))

    def _resolve_turn_toolsets(self, user_config: dict, source: SessionSource, platform_key: str):
        """``(enabled_toolsets, disabled_toolsets)`` for an agent run on ``source``."""
        from agent.skill_utils import parse_config_string_list
        enabled = self._resolve_enabled_toolsets_for_source(user_config, source, platform_key)
        disabled = parse_config_string_list((user_config.get("agent") or {}).get("disabled_toolsets")) or None
        return enabled, disabled



    def _get_proxy_url(self) -> Optional[str]:
        """Proxy URL if proxy mode is configured (GATEWAY_PROXY_URL env wins over ``gateway.proxy_url``).
        Per-profile like GATEWAY_PROXY_KEY: under multiplex a raw environ read would ship a secondary's
        turns (authenticated with ITS scoped key) to the default profile's proxy. Same fallback shape as
        the key — only ``UnscopedSecretError`` (the unscoped default-profile path) reads the env."""
        from gateway.run import _load_gateway_config
        from agent.secret_scope import UnscopedSecretError, get_secret
        try:
            url = (get_secret("GATEWAY_PROXY_URL") or "").strip()
        except UnscopedSecretError:
            url = os.getenv("GATEWAY_PROXY_URL", "").strip()
        if not url:
            url = ((_load_gateway_config().get("gateway") or {}).get("proxy_url") or "").strip()
        return url.rstrip("/") if url else None

    def _build_stream_consumer_config(
        self, source: SessionSource, scfg: Any, adapter: Any, *, on_missing_cursor: str,
    ) -> tuple[Any, Optional[Callable[[], None]]]:
        """Build the shared ``StreamConsumerConfig`` and optional Telegram pause-typing closure.
        For non-editing adapters ``on_missing_cursor="fallback"`` streams with an empty cursor;
        ``"raise"`` raises ``RuntimeError`` so the caller skips streaming entirely."""
        from gateway.stream_consumer import StreamConsumerConfig
        _pause_typing_before_finalize = None
        if source.platform == Platform.TELEGRAM and hasattr(adapter, "pause_typing_for_chat"):
            def _pause_typing_before_finalize(_adapter=adapter, _chat_id=source.chat_id) -> None:
                call_typing_with_metadata(_adapter.pause_typing_for_chat, _chat_id, metadata=self._thread_metadata_for_source(source))
        # Non-editing platforms (QQ, WeChat) skip streaming — the partial first message could never
        # be updated — unless they have a native-streaming transport (WeCom msgtype "stream").
        _adapter_supports_edit = getattr(adapter, "SUPPORTS_MESSAGE_EDITING", True)
        _adapter_supports_native_stream = bool(getattr(adapter, "SUPPORTS_NATIVE_STREAMING", False))
        if not _adapter_supports_edit and not _adapter_supports_native_stream and on_missing_cursor == "raise":
            raise RuntimeError("skip streaming for non-editable platform")
        _effective_cursor = scfg.cursor if _adapter_supports_edit else ""
        # Some Matrix clients render the cursor as tofu: stream text, no cursor.
        if source.platform == Platform.MATRIX:
            _effective_cursor = ""
        # Fresh-final applies to Telegram only (others edit in place cheaply).
        # Fresh-final applies to Telegram only — other platforms either edit in place cheaply (Discord,
        # Slack) or don't have the timestamp-on-edit / edit-timestamp-stays-stale problem. (Ported from
        # openclaw/openclaw#72038.)
        _fresh_final_secs = (
            float(getattr(scfg, "fresh_final_after_seconds", 0.0) or 0.0)
            if source.platform == Platform.TELEGRAM else 0.0
        )
        _consumer_cfg = StreamConsumerConfig(
            edit_interval=scfg.edit_interval, buffer_threshold=scfg.buffer_threshold,
            cursor=_effective_cursor,
            fresh_final_after_seconds=_fresh_final_secs, transport=scfg.transport or "edit",
            chat_type=getattr(source, "chat_type", "") or "",
            strip_trailing_silence_markers=(source.platform != Platform.WEBHOOK),
        )
        return _consumer_cfg, _pause_typing_before_finalize

    def _run_still_current_fn(self, session_key: Optional[str], run_generation: Optional[int]) -> Callable[[], bool]:
        """Predicate: does this run's generation still own ``session_key``? (always True when untracked)."""
        def _run_still_current() -> bool:
            if run_generation is None or not session_key:
                return True
            return self._is_session_run_current(session_key, run_generation)
        return _run_still_current

    @staticmethod
    def _proxy_error_result(text: str) -> dict[str, Any]:
        return {"final_response": text, "messages": [], "api_calls": 0, "tools": []}

    def _proxy_stream_consumer(self, source: SessionSource, event_message_id, _thread_metadata, _run_still_current):
        """Platform stream consumer for the proxy path when streaming is enabled, else ``None``."""
        from gateway.run import _load_gateway_config, _platform_config_key
        from gateway.run_config_loaders import streaming_config_for_source
        _scfg = streaming_config_for_source(self, source)
        # #60671 — streaming TTS consumer is created on the outer event-loop thread before run_sync
        # launches.  run_sync only reads it via ``streaming_tts_consumer_holder[0]`` for delta callback
        # wiring.
        # Global master switch first: skips the config.yaml re-read on the default (off) path.
        if not _scfg.globally_enabled:
            return None
        from gateway.display_config import resolve_display_setting
        _plat_streaming = resolve_display_setting(_load_gateway_config(), _platform_config_key(source.platform), "streaming")
        if not _scfg.enabled_for(_plat_streaming):
            return None
        try:
            from gateway.stream_consumer import GatewayStreamConsumer
            _adapter = self._delivery_adapter_for(source)
            if not _adapter:
                return None
            _consumer_cfg, _pause_typing_before_finalize = self._build_stream_consumer_config(
                source, _scfg, _adapter, on_missing_cursor="fallback",
            )
            return GatewayStreamConsumer(
                adapter=_adapter, chat_id=source.chat_id, config=_consumer_cfg,
                metadata=_thread_metadata, on_before_finalize=_pause_typing_before_finalize,
                initial_reply_to_id=event_message_id, run_still_current=_run_still_current,
            )
        except Exception as _sc_err:
            logger.debug("Proxy: could not set up stream consumer: %s", _sc_err)
            return None

    async def _run_agent_via_proxy(
        self, message: str, context_prompt: str, history: list[dict[str, Any]],
        source: SessionSource, session_id: str, session_key: str | None = None,
        run_generation: Optional[int] = None, event_message_id: Optional[str] = None,
        scheduled_heartbeat: bool = False,
    ) -> dict[str, Any]:
        """Forward the message to a remote Hermes API server instead of running a local AIAgent.

        Lets a Docker container handle Matrix E2EE while the agent runs on the host with full
        access to local files, memory, skills, and a unified session store."""
        from gateway.run import _GATEWAY_PROXY_SSE_BUFFER_MAX_CHARS
        try:
            from aiohttp import ClientSession as _AioClientSession, ClientTimeout
        except ImportError:
            return self._proxy_error_result(t("gateway.proxy.requires_aiohttp", hint=install_hint("messaging")))

        from gateway.platforms.base import describe_proxy_error, safe_proxy_url_for_log
        proxy_url = self._get_proxy_url()
        if not proxy_url:
            return self._proxy_error_result(t("gateway.proxy.url_missing"))

        # The proxy key is a per-profile credential: honor the installed secret scope under multiplex.
        # Only UnscopedSecretError (the unscoped default-profile path) falls back to the env; any
        # other get_secret() error propagates (same as BASE) rather than silently degrading to the
        # ambient key, which may hold another profile's credential.
        from agent.secret_scope import UnscopedSecretError, get_secret

        try:
            proxy_key = (get_secret("GATEWAY_PROXY_KEY") or "").strip()
        except UnscopedSecretError:
            proxy_key = os.getenv("GATEWAY_PROXY_KEY", "").strip()

        _run_still_current = self._run_still_current_fn(session_key, run_generation)

        def _stale_result(what: str) -> dict[str, Any]:
            logger.info(
                "Discarding stale proxy %s for %s — generation %d is no longer current",
                what, session_key or "?", run_generation or 0,
            )
            return {
                "final_response": "", "messages": [], "api_calls": 0, "tools": [],
                "history_offset": len(history), "session_id": session_id, "response_previewed": False,
            }

        # OpenAI chat format. The remote keeps continuity via X-Hermes-Session-Id; send the current
        # message plus a compact text-only history for a remote that has none yet.
        api_messages: list[dict[str, str]] = [{"role": "system", "content": context_prompt}] if context_prompt else []
        api_messages += [
            {"role": msg.get("role"), "content": msg.get("content")}
            for msg in history if msg.get("role") in {"user", "assistant"} and msg.get("content")
        ]
        api_messages.append({"role": "user", "content": message})

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if proxy_key:
            headers["Authorization"] = f"Bearer {proxy_key}"
        if session_id:
            headers["X-Hermes-Session-Id"] = session_id
        body = {"model": "hermes-agent", "messages": api_messages, "stream": True}

        _thread_metadata: Optional[dict[str, Any]] = self._thread_metadata_for_source(source, event_message_id)
        _stream_consumer = (
            None if scheduled_heartbeat
            else self._proxy_stream_consumer(source, event_message_id, _thread_metadata, _run_still_current)
        )
        from gateway.outbox import active_turn, run_turn_child
        stream_task = (asyncio.create_task(run_turn_child(_stream_consumer.run(), active_turn()))
                       if _stream_consumer else None)

        _adapter = self._delivery_adapter_for(source)
        if _adapter and not scheduled_heartbeat:
            with suppress(Exception):
                await _adapter.send_typing(source.chat_id, metadata=typing_metadata_for_turn(
                    _adapter, source.chat_id, event_message_id, _thread_metadata))

        full_response = ""
        _start = time.time()
        saw_done = False

        def _consume_sse_line(line: str) -> bool:
            """Parse one SSE line into full_response; True when the terminal ``[DONE]`` was seen.

            Malformed frames (bad JSON, ``choices: [null]``, non-dict deltas) are skipped —
            one bad chunk must not abort the whole stream."""
            nonlocal full_response
            line = line.strip()
            if not line.startswith("data: "):
                return False
            data = line[6:]
            if data.strip() == "[DONE]":
                return True
            try:
                choices = json.loads(data).get("choices") or []
                content = choices[0].get("delta", {}).get("content", "") if choices else ""
            except (json.JSONDecodeError, TypeError, AttributeError, IndexError):
                return False
            if content:
                full_response += content
                if _stream_consumer:
                    _stream_consumer.on_delta(content)
            return False

        try:
            # sock_connect bounds the TCP connect phase so an unreachable proxy host
            # (DNS fail, firewall, remote down) fails fast instead of hanging on the OS default.
            _timeout = ClientTimeout(total=0, sock_read=1800, sock_connect=30)
            async with _AioClientSession(timeout=_timeout) as session:
                async with session.post(f"{proxy_url}/v1/chat/completions", json=body, headers=headers) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.warning("Proxy error (%d) from %s: %s", resp.status, safe_proxy_url_for_log(proxy_url), error_text[:500])
                        return self._proxy_error_result(t("gateway.proxy.http_error", status=resp.status, error=error_text[:300]))

                    buffer = ""
                    async for chunk in resp.content.iter_any():
                        if saw_done:
                            # A buggy upstream that holds the connection open after [DONE]
                            # would otherwise block us for up to sock_read seconds.
                            break
                        if not _run_still_current():
                            return _stale_result("stream")
                        buffer += chunk.decode("utf-8", errors="replace")
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            if _consume_sse_line(line):
                                saw_done = True
                                break
                        if saw_done:
                            break
                        if len(buffer) > _GATEWAY_PROXY_SSE_BUFFER_MAX_CHARS:
                            raise ValueError("Proxy SSE stream exceeded max buffer size without a line boundary")
                    # The final SSE frame may not be newline-terminated: flush the residual
                    # buffer after EOF instead of silently dropping its content.
                    if not saw_done and buffer:
                        saw_done = _consume_sse_line(buffer)
                    if not saw_done:
                        # Clean EOF without [DONE] — the upstream dropped the response
                        # mid-stream. Keep any partial text but say so instead of
                        # presenting the truncation as a complete answer.
                        logger.warning(
                            "Proxy SSE stream from %s ended without [DONE] — response may be truncated "
                            "(%d chars received)", safe_proxy_url_for_log(proxy_url), len(full_response),
                        )
                        if not full_response:
                            return self._proxy_error_result(t("gateway.proxy.closed_early"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            shown = describe_proxy_error(e, proxy_url) or str(e)
            logger.error("Proxy connection error to %s: %s", safe_proxy_url_for_log(proxy_url), shown)
            if not full_response:
                return self._proxy_error_result(t("gateway.proxy.connection_error", error=shown))
            # Partial response — return what we got
        finally:
            if _stream_consumer:
                _stream_consumer.finish()
            if stream_task:
                try:
                    await asyncio.wait_for(stream_task, timeout=5.0)
                except (TimeoutError, asyncio.CancelledError):
                    stream_task.cancel()

        _elapsed = time.time() - _start
        if not _run_still_current():
            return _stale_result("result")
        logger.info(
            "proxy response: url=%s session=%s time=%.1fs response=%d chars",
            safe_proxy_url_for_log(proxy_url), (session_id or "")[:20], _elapsed, len(full_response),
        )
        return {
            "final_response": full_response or t("gateway.proxy.no_response"),
            "messages": [
                {"role": "user", "content": message},
                {"role": "assistant", "content": full_response},
            ],
            "api_calls": 1,
            "tools": [],
            "history_offset": len(history),
            "session_id": session_id,
            "response_previewed": _stream_consumer is not None and bool(full_response),
        }

    async def _run_agent(
        self, message: str, context_prompt: str, history: list[dict[str, Any]],
        source: SessionSource, session_id: str, **turn_kwargs,
    ) -> dict[str, Any]:
        """Profile-scoping wrapper around ``_run_agent_inner`` (same keyword parameters; pass-through
        when multiplexing is off)."""
        with self._profile_scope_for_source(source):
            return await self._run_agent_inner(message, context_prompt, history, source, session_id, **turn_kwargs)

    def _run_agent_display_settings(self, source: SessionSource) -> GatewayRunner._RunAgentDisplay:
        """Resolve per-platform display, progress, status and streaming-surface settings for a turn."""
        from gateway.run import (
            _gateway_platform_value, _has_platform_display_override, _load_gateway_config,
            _platform_config_key,
        )
        from agent.secret_scope import get_secret
        from gateway.display_config import resolve_display_setting, resolve_tool_progress
        from gateway.status_phrases import choose_status_phrase, resolve_status_phrase_catalog
        from gateway.session_policy import policy_for_source, policy_turn_toolsets
        policy = policy_for_source(self, source)
        from gateway.session_authorities import active_authority
        user_config = policy.config(active_authority(self), secrets=False) if policy else _load_gateway_config()
        platform_key = policy.platform if policy else _platform_config_key(source.platform)
        enabled_toolsets, disabled_toolsets = self._resolve_turn_toolsets(user_config, source, platform_key)
        if policy:
            enabled_toolsets = policy_turn_toolsets(active_authority(self), policy, user_config, source.chat_id)
        adapter = self._delivery_adapter_for(source)
        # Tool preview length (0 = no limit) and friendly tool labels (default on), per-platform.
        for _setter, _setting, _default, _cast in (
            ("set_tool_preview_max_len", "tool_preview_length", 0, lambda v: int(v) if v else 0),
            ("set_friendly_tool_labels", "friendly_tool_labels", True, bool),
        ):
            with suppress(Exception):
                from agent import display as _agent_display
                _val = resolve_display_setting(user_config, platform_key, _setting, _default)
                getattr(_agent_display, _setter)(_cast(_val))

        # Resolve the mode and its provenance together: null inherits, tier off is not intent.
        # A raw os.getenv here reads whichever profile's env loaded last under multiplexing
        # (#116898); get_secret resolves through the active profile's scope instead.
        progress_mode, _tool_progress_explicit = resolve_tool_progress(
            user_config, platform_key, get_secret("HERMES_TOOL_PROGRESS_MODE"),
        )
        # "accumulate" (edit one bubble) or "separate" (one msg per tool)
        progress_grouping = resolve_display_setting(user_config, platform_key, "tool_progress_grouping") or "accumulate"
        _generic_status_recent: list[str] = []
        _generic_status_catalog = resolve_status_phrase_catalog(user_config, platform_key)

        def _display_surface_mode(
            setting: str, *, default: bool = False,
            require_platform_override_for: set[Any] | None = None, allow_generic: bool = False,
        ) -> str:
            """Return off|raw|generic for a gateway visibility surface."""
            if require_platform_override_for:
                current_platform = _gateway_platform_value(source.platform)
                platform_only = {_gateway_platform_value(item) for item in require_platform_override_for}
                if (
                    current_platform in platform_only
                    and not _has_platform_display_override(user_config, platform_key, setting)
                ):
                    return "off"
            value = resolve_display_setting(user_config, platform_key, setting, default)
            if isinstance(value, str) and value.strip().lower() == "generic":
                return "generic" if allow_generic else "off"
            return "raw" if bool(value) else "off"

        def _generic_status_phrase(kind: str, *, tool_name: str | None = None, preview: str | None = None, args: Any = None) -> str:
            try:
                return choose_status_phrase(
                    kind, tool_name=tool_name, preview=preview, args=args,
                    recent=_generic_status_recent, catalog=_generic_status_catalog,
                )
            except Exception as _phrase_err:
                logger.debug("generic status phrase selection failed: %s", _phrase_err)
                return (t("gateway.progress.status_fallback_long")
                        if kind in {"heartbeat", "waiting", "long_running", "status"}
                        else t("gateway.progress.status_fallback_short"))

        # Webhooks can't edit messages; a bot peer gets the final reply only (#137027): no progress lanes.
        is_webhook, bot_peer = source.platform == Platform.WEBHOOK, not automatic_notices_allowed(source)
        tool_progress_enabled = progress_mode not in {"off", "log"} and not is_webhook and not bot_peer
        # Live status for text-rendering typing indicators (Slack); independent of tool_progress.
        _live_status_mode = resolve_display_setting(user_config, platform_key, "live_status", "full")
        _live_status_adapter = (
            adapter if getattr(adapter, "supports_status_text", False) and _live_status_mode != "off" else None
        )
        # "log" mode: tool calls go to ~/.hermes/logs/tool_calls.log instead of the chat. Gateway-only.
        log_mode_enabled = progress_mode == "log" and not is_webhook
        # Interim assistant messages and thinking_progress are independent of tool progress (same
        # queue). Mattermost requires a per-platform opt-in: scratch text leaks into public threads.
        interim_assistant_messages_mode = _display_surface_mode(
            "interim_assistant_messages", default=True, require_platform_override_for={Platform.MATTERMOST},
        )
        interim_assistant_messages_enabled = not is_webhook and not bot_peer and interim_assistant_messages_mode != "off"
        _thinking_enabled = _display_surface_mode(
            "thinking_progress", default=False, require_platform_override_for={Platform.MATTERMOST},
        ) != "off" and not bot_peer
        # Slack-native task cards (#29483): when the Slack adapter's opt-in is set, tool progress renders as
        # native plan/task cards via chat.startStream — the progress queue is needed even though Slack keeps
        # ordinary text tool_progress off by default (requiring both flags would silently leave the native
        # feature inactive).
        # Cards are still tool progress. Slack's TIER default (``off``) only quiets the text lane so
        # cards stay on for unconfigured installs, but an operator who WRITES ``tool_progress: off``
        # (global, platform override, or legacy overrides) has asked for no tool progress at all and
        # gets no cards either. Every other explicit mode keeps the card lane.
        _native_slack_task_cards = False
        if (
            source.platform == Platform.SLACK
            and hasattr(adapter, "native_task_cards_enabled")
            and not (_tool_progress_explicit and progress_mode == "off") and not bot_peer
        ):
            try:
                _native_slack_task_cards = bool(adapter.native_task_cards_enabled())
            except Exception:
                logger.debug("Slack native task-card config check failed", exc_info=True)
        return self._RunAgentDisplay(
            user_config=user_config, platform_key=platform_key, enabled_toolsets=enabled_toolsets,
            disabled_toolsets=disabled_toolsets, resolve_display_setting=resolve_display_setting,
            progress_mode=progress_mode, progress_grouping=progress_grouping,
            _display_surface_mode=_display_surface_mode,
            tool_progress_enabled=tool_progress_enabled, _live_status_mode=_live_status_mode,
            _live_status_adapter=_live_status_adapter, log_mode_enabled=log_mode_enabled,
            log_queue=queue.Queue() if log_mode_enabled else None,
            interim_assistant_messages_enabled=interim_assistant_messages_enabled,
            _thinking_enabled=_thinking_enabled, _native_slack_task_cards=_native_slack_task_cards,
            needs_progress_queue=tool_progress_enabled or _thinking_enabled or _native_slack_task_cards,
            _generic_status_phrase=_generic_status_phrase,
        )

    # _RunAgentDisplay fields copied verbatim onto the TurnContext.
    _DISPLAY_TO_TURN_CTX = (
        "_live_status_adapter", "_live_status_mode", "_thinking_enabled", "progress_mode",
        "progress_grouping", "tool_progress_enabled", "log_queue", "resolve_display_setting",
        "user_config", "enabled_toolsets", "disabled_toolsets", "log_mode_enabled",
        "interim_assistant_messages_enabled", "needs_progress_queue", "_native_slack_task_cards",
    )


    def _run_agent_bind_turn_wiring(
        self, turn_ctx: TurnContext, turn_runner: TurnRunner, source: SessionSource,
        event_message_id: Optional[str], _native_slack_task_cards: bool,
    ) -> Optional[dict[str, Any]]:
        """Resolve progress threading, then publish progress metadata and the sync→async bridges onto
        ``turn_ctx`` (the one-slot holders shared with run_sync's executor thread are TurnContext
        defaults). Returns ``_status_thread_metadata``."""
        turn_ctx._progress_metadata, turn_ctx._progress_reply_to, _status_thread_metadata = (
            self._run_agent_progress_threading(source, event_message_id, _native_slack_task_cards)
        )
        # Bridges: sync step/event/status callbacks → async hooks.emit and adapter.send.
        turn_ctx._loop_for_step = asyncio.get_running_loop()
        turn_ctx._hooks_ref = self.hooks
        turn_ctx._step_callback_sync = turn_runner._step_callback_sync
        turn_ctx._event_callback_sync = turn_runner._event_callback_sync
        turn_ctx._status_callback_sync = turn_runner._status_callback_sync
        turn_ctx.bind_status_adapter(lambda: self._delivery_adapter_for(source))
        turn_ctx._status_chat_id = source.chat_id
        turn_ctx._status_thread_metadata = _status_thread_metadata
        return _status_thread_metadata

    async def _run_agent_notify_long_running(
        self, disp: GatewayRunner._RunAgentDisplay, turn_ctx: TurnContext, _executor_task_holder: list,
    ) -> None:
        """Periodic "still working" heartbeat, edited in place where supported. Stops once this run
        no longer owns the session slot or the executor finished. ``_executor_task_holder[0]`` is
        bound just after this task is scheduled (reads as None until then).

        Interval: agent.gateway_notify_interval / HERMES_AGENT_NOTIFY_INTERVAL (default 180s; 0 or
        long_running_notifications=off disables)."""
        from gateway.run import _float_env, _interim_metadata, _non_conversational_metadata
        _notify_start = time.time()
        _NOTIFY_INTERVAL = _float_env("HERMES_AGENT_NOTIFY_INTERVAL", 180)
        _long_running_mode = disp._display_surface_mode("long_running_notifications", default=True, allow_generic=True)
        if _NOTIFY_INTERVAL <= 0 or _long_running_mode == "off":
            return
        source, session_key, agent_holder = turn_ctx.source, turn_ctx.session_key, turn_ctx.agent_holder
        _status_thread_metadata = turn_ctx._status_thread_metadata
        if not (_notify_adapter := self._delivery_adapter_for(source)):
            return
        _heartbeat_msg_id: Optional[str] = None
        while True:
            await asyncio.sleep(_NOTIFY_INTERVAL)
            if not self._should_emit_long_running_notification(
                session_key, agent_holder[0], _executor_task_holder[0]
            ):
                break
            _notify_adapter = self._delivery_adapter_for(source) or _notify_adapter  # reconnects retire it (#138133)
            _elapsed_mins = int((time.time() - _notify_start) // 60)
            # Terse heartbeat by default; the iteration counter is gated on busy_ack_detail.
            _status_detail = ""
            _want_iteration_detail = bool(
                disp.resolve_display_setting(disp.user_config, disp.platform_key, "busy_ack_detail", True)
            )
            _a = self._agent_activity_summary(agent_holder[0])
            with suppress(Exception):
                if _a:
                    _parts = []
                    if _want_iteration_detail:
                        _parts.append(format_iteration_progress(_a["api_call_count"], _a["max_iterations"]))
                    _action = _a.get("current_tool") or _a.get("last_activity_desc")
                    if _action:
                        _parts.append(str(_action))
                    if _parts:
                        _status_detail = " — " + ", ".join(_parts)
            _heartbeat_text = (
                disp._generic_status_phrase("status")
                if _long_running_mode == "generic"
                else t("gateway.progress.working_heartbeat", minutes=_elapsed_mins, detail=_status_detail)
            )
            try:
                _notify_res = None
                if _heartbeat_msg_id:
                    try:
                        _notify_res = await _notify_adapter.edit_message(source.chat_id, _heartbeat_msg_id, _heartbeat_text)
                    except Exception as _ee:
                        logger.debug("Heartbeat edit failed: %s", _ee)
                        _notify_res = None
                if not (_notify_res and getattr(_notify_res, "success", False)):
                    # The edit above awaited; a drain/restart notice may have gone out meanwhile, and
                    # a fresh "Working" bubble after it reads as a contradiction (#10990).
                    if not self._should_emit_long_running_notification(
                        session_key, agent_holder[0], _executor_task_holder[0]
                    ):
                        break
                    _notify_res = await _notify_adapter.send(
                        source.chat_id, _heartbeat_text,
                        metadata=_interim_metadata(_non_conversational_metadata(_status_thread_metadata, platform=source.platform)),
                    )
                    if getattr(_notify_res, "success", False) and getattr(_notify_res, "message_id", None):
                        _heartbeat_msg_id = str(_notify_res.message_id)
                        if turn_ctx._cleanup_progress:
                            turn_ctx.track_cleanup_id(_heartbeat_msg_id)
            except Exception as _ne:
                logger.debug("Long-running notification error: %s", _ne)

    async def _run_agent_silence_followup(self, response: dict, turn_ctx: TurnContext) -> dict:
        from gateway.run_turn_silence_recovery import run_silence_followup
        return await run_silence_followup(self, response, turn_ctx)

    async def _run_agent_inner(
        self, message: str, context_prompt: str, history: list[dict[str, Any]],
        source: SessionSource, session_id: str, session_key: str | None = None,
        run_generation: Optional[int] = None, _interrupt_depth: int = 0,
        event_message_id: Optional[str] = None, inbound_message_id: Optional[str] = None,
        recovery_id: Optional[str] = None, recovery_session_key: Optional[str] = None,
        channel_prompt: Optional[str] = None, internal: bool = False, event_metadata: Optional[dict] = None,
        moa_config: Optional[dict] = None,
        persist_user_message: Optional[Any] = None, persist_user_timestamp: Optional[float] = None,
        persist_user_display_kind: Optional[str] = None, message_type: Optional[str] = None,
        persist_user_display_metadata: Optional[dict] = None,
        reply_expected: Optional[bool] = None,
        scheduled_heartbeat: bool = False,
        _post_delivery_adapter: Optional[BasePlatformAdapter] = None,
        title_user_message: Optional[str] = None,
        _silence_retry: bool = False,
    ) -> dict[str, Any]:
        """Run the agent; returns the full run_conversation result dict.

        Keys: "final_response", "messages", "api_calls", "completed"."""
        if self._get_proxy_url():
            # Local-only recovery: the proxy API requires a new non-empty conversation row,
            # not the empty internal continuation. Persist the recovered input before dispatch
            # so a gateway crash cannot replay its side effects on the next boot.
            _proxy_user_row_persisted = await self._hmwa_persist_proxy_recovery_user_row(
                session_id=session_id, message=message, inbound_message_id=inbound_message_id,
                recovery_id=recovery_id, recovery_session_key=recovery_session_key,
                persist_user_message=persist_user_message, persist_user_timestamp=persist_user_timestamp,
                persist_user_display_kind=persist_user_display_kind,
                persist_user_display_metadata=persist_user_display_metadata,
            )
            _proxy_result = await self._run_agent_via_proxy(
                message=message, context_prompt=context_prompt, history=history, source=source,
                session_id=session_id, session_key=session_key, run_generation=run_generation,
                event_message_id=event_message_id, scheduled_heartbeat=scheduled_heartbeat,
            )
            if _proxy_user_row_persisted:
                _proxy_result["_gateway_user_turn_persisted"] = True
            return _proxy_result

        from run_agent import AIAgent

        disp = await asyncio.to_thread(self._run_agent_display_settings, source)
        from gateway.session_policy import policy_for_source
        if session_key and policy_for_source(self, source) is None:
            await self._rehydrate_session_model_selection_async(session_key)
            await self._expire_attended_temporary_picks_async(session_key, disp.user_config)
        cron_review = (event_metadata or {}).get("_cron_review_capture") is not None
        if scheduled_heartbeat or cron_review:
            # A heartbeat is proactive work: tool chrome, drafts, thinking and periodic
            # liveness notices would create a user-visible ping before its final result is known.
            # Keep status callbacks intact for approvals and actionable failures. An invisible
            # cron review turn stays equally private until its report is presented.
            disp = dataclasses.replace(
                disp, tool_progress_enabled=False, _thinking_enabled=False, _native_slack_task_cards=False,
                needs_progress_queue=False,
            )
        if session_key:
            from gateway.model_pick import model_pick_deadline
            await self._rehydrate_session_model_override_async(session_key)
            deadline = model_pick_deadline(self._session_model_override(session_key))
            if deadline and time.time() >= deadline:
                # Expire before the assignment snapshot, and publish its notice on this loop
                # before the worker can consume it on the same turn.
                await self._resolve_session_agent_runtime_async(
                    source=source, session_key=session_key, user_config=disp.user_config)
        # Hydrate the /reasoning pick off the loop; the assignment below is captured synchronously.
        reasoning_override = await self._session_reasoning_override_async(session_key)
        turn_ctx, turn_runner, _cleanup_adapter = self._run_agent_build_turn_context(
            disp, AIAgent, message=message, source=source, session_key=session_key,
            run_generation=run_generation, reasoning_override=reasoning_override,
            context_prompt=context_prompt, history=history,
            session_id=session_id, _interrupt_depth=_interrupt_depth,
            event_message_id=event_message_id, inbound_message_id=inbound_message_id,
            recovery_id=recovery_id,
            channel_prompt=channel_prompt, moa_config=moa_config, internal=internal,
            event_metadata=dict(event_metadata or {}),
            title_user_message=title_user_message,
            persist_user_message=persist_user_message,
            persist_user_timestamp=persist_user_timestamp,
            persist_user_display_kind=persist_user_display_kind,
            reply_expected=reply_expected,
            persist_user_display_metadata=persist_user_display_metadata, scheduled_heartbeat=scheduled_heartbeat,
            silence_retry=_silence_retry,
            _post_delivery_adapter=_post_delivery_adapter,
            voice_turn=str(getattr(message_type, "value", message_type) or "").lower() == "voice",
        )
        turn_ctx.mute_notification_reply |= cron_review
        _status_thread_metadata = self._run_agent_bind_turn_wiring(
            turn_ctx, turn_runner, source, event_message_id, disp._native_slack_task_cards,
        )
        # Two independent quiet reasons: a muted diagnostic wake (ours, or a cron review) and a
        # scheduled heartbeat.
        if not (scheduled_heartbeat or turn_ctx.mute_notification_reply):
            self._run_agent_start_streaming_tts(
                source, message_type, _status_thread_metadata, turn_ctx.streaming_tts_consumer_holder,
            )

        # Progress sender drains BOTH tool-progress lines and thinking bubbles (needs_progress_queue).
        spawn = asyncio.create_task
        progress_task = spawn(turn_runner.send_progress_messages()) if disp.needs_progress_queue else None
        log_task = spawn(self._run_agent_write_tool_log(disp.log_queue)) if disp.log_mode_enabled else None
        # The stream consumer is created inside run_sync; this task polls for it.
        stream_task = spawn(self._run_agent_stream_consumer_task(turn_ctx.stream_consumer_holder))
        tracking_task = spawn(self._run_agent_track_agent(turn_ctx))
        _interrupt_detected = asyncio.Event()  # shared with backup check
        interrupt_monitor = spawn(self._run_agent_monitor_for_interrupt(turn_ctx, _interrupt_detected))
        # Periodic "still working" notifications so the user knows the agent hasn't died.
        _executor_task_holder: list = [None]  # bound once the executor future exists (see below)
        _notify_task = (
            None if (scheduled_heartbeat or turn_ctx.mute_notification_reply or not automatic_notices_allowed(source))
            else spawn(self._run_agent_notify_long_running(disp, turn_ctx, _executor_task_holder))
        )

        worker = None
        try:
            # run_sync is TurnRunner.run_sync (bound method; executor call unchanged).
            worker = self._run_agent_start_turn_worker(turn_ctx, turn_runner.run_sync)
            _executor_task_holder[0] = worker.executor_task  # read late by _notify_long_running
            response = await self._run_agent_await_turn_worker(worker, turn_ctx, _interrupt_detected, interrupt_monitor)
            if isinstance(response, dict) and "delivery_text" in response: response = {**response, "final_response": response["delivery_text"]}
            if isinstance(response, dict):
                response["_notification_reply_muted"] = turn_ctx.mute_notification_reply
            self._run_agent_evict_on_fallback(turn_ctx)

            # Interrupted OR queued message (/queue)?
            result = turn_ctx.result_holder[0]
            adapter = self._delivery_adapter_for(source)
            await self._run_agent_finalize_streaming_tts(turn_ctx, adapter)
            # A queued successor already supplies a fresh turn; drain it before considering recovery.
            pending_event, pending = await self._run_agent_drain_pending(result, adapter, source, session_key)
            pending_event, pending = defer_review_followup(self, turn_ctx, adapter, pending_event, pending)
            from gateway.run_turn_silence_recovery import apply_terminal_reply_metadata, silence_recovery_due
            apply_terminal_reply_metadata(turn_ctx, response, result)
            if pending_event or pending:
                await settle_turn_senders(progress_task, _notify_task)  # their bubble ids join this turn's cleanup
                return await self._run_agent_queued_followup(
                    turn_ctx, adapter, pending, pending_event, response, result, stream_task,
                )
            if silence_recovery_due(self, response, turn_ctx, silence_retry=_silence_retry,
                                    scheduled_heartbeat=scheduled_heartbeat):
                # End-of-turn filtering must finish BEFORE a new consumer can preview the retry.
                if turn_ctx.stream_consumer_holder[0] is not None:
                    await self._await_stream_task(stream_task)
                if turn_ctx._run_still_current():
                    return await self._run_agent_silence_followup(response, turn_ctx)
        finally:
            try:
                await self._run_agent_cleanup_turn_tasks(
                    turn_ctx, progress_task=progress_task, log_task=log_task, interrupt_monitor=interrupt_monitor,
                    _notify_task=_notify_task, tracking_task=tracking_task, stream_task=stream_task,
                )
            finally:
                if worker is not None:
                    worker.turn_done.set()
                    self._release_finished_turn_agent(turn_ctx, worker)

        await self._run_agent_mark_streamed_delivery(response, turn_ctx)
        self._run_agent_schedule_bubble_cleanup(response, _cleanup_adapter, turn_ctx)
        if isinstance(response, dict):
            # Recovery may be redirected after it starts. The outer silence contract is separate
            # from the inbound message that owns this execution's terminal delivery.
            response["_terminal_inbound_id"] = turn_ctx.inbound_message_id
        return response
