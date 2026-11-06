# health: allow FILE_LINES -- strict session-pin and loop-effect integration lives in this established turn facade; follow-up extraction is required before adding unrelated behavior.
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
from gateway.config import Platform
from gateway.cron_review import defer_review_followup
from gateway.media_repair import repair_explicit_computer_use_media_paths
from gateway.platforms.base import BasePlatformAdapter, ProcessingOutcome
from gateway.platforms.event import MessageEvent, turn_origin_for_event
from gateway.platforms.typing_state import call_typing_with_metadata
from gateway.session_context import bound_turn_origin, set_turn_origin
from gateway.run_turn_hygiene import hygiene_no_commit_reason
from gateway.run_turn_pending import _drain_leftover_steer
from gateway.run_loop_wake import GatewayLoopWakeMixin
from gateway.run_turn_persistence import GatewayTurnPersistenceMixin, _acp_launch
from gateway.platforms.helpers import typing_metadata_for_turn
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


class GatewayTurnMixin(GatewayLoopWakeMixin, GatewayBackgroundMixin, GatewayTurnPrepareMixin, GatewayTurnHygieneMixin, GatewayTurnPersistenceMixin):
    """Agent-turn execution for GatewayRunner (see module docstring)."""



    def _load_attended_activity_stamp(self, session_key: str, *, refresh: bool = False):
        """Read the durable activity stamp without changing live session state."""
        with suppress(Exception):
            stamp = self.session_store.get_last_user_activity_at(session_key, refresh=refresh)
            if stamp is not None and hasattr(stamp, "timestamp"):
                stamp = stamp.timestamp()
            return stamp
        return None

    def _load_attended_route_active(self, session_key: str) -> Optional[bool]:
        """Read the last route that actually reached an API call, if persisted."""
        with suppress(Exception):
            return self.session_store.get_attended_route_active(session_key)
        return None

    def _ensure_attended_new_session_activity(self, session_key: str, cfg, stamp):
        """Start the attended window for a newly opened session without persisting automation as user activity."""
        state = self._peek_session_state(session_key)
        new_session = bool(
            state is not None
            and getattr(state.conversation, "attended_new_session", False)
            and cfg.start_new_sessions
        )
        if new_session and stamp is None:
            # This is intentionally process-local: an internal/automation first turn starts on
            # the attended route, but must not create Brian's durable genuine-activity stamp.
            stamp = time.time()
        return stamp, new_session

    def _load_attended_resolution_snapshot(
        self, session_key: str, model: str, attended_model: str, user_config: Optional[dict] = None,
    ) -> dict:
        """Read durable attended state and config-derived reasoning off the gateway loop."""
        if user_config is not None:
            with suppress(Exception):
                from gateway.run import _resolve_gateway_model
                model = model or _resolve_gateway_model(user_config)
        durable_stamp = stamp = self._load_attended_activity_stamp(session_key, refresh=True)
        cfg = parse_attended_config(user_config or {})
        stamp, new_session = self._ensure_attended_new_session_activity(session_key, cfg, stamp)
        reasoning_override = None
        reasoning_scope = "session"
        store = getattr(self, "session_store", None)
        if store is not None:
            with suppress(Exception):
                from gateway.session import sanitize_reasoning_override
                reasoning_override = sanitize_reasoning_override(store.get_reasoning_override(session_key))
                reasoning_scope = store.get_reasoning_override_scope(session_key)
        default_reasoning = {}
        attended_reasoning = {}
        with suppress(Exception):
            default_reasoning = self._load_reasoning_config(model) or {}
        if attended_model:
            with suppress(Exception):
                attended_reasoning = self._load_reasoning_config(attended_model) or {}
        from gateway.session_attended import session_attended_pin_until, session_attended_pinned
        return {
            "activity_stamp": stamp,
            # Switch-back gating reads these on the loop; the process-local new-session stamp
            # above must not stand in for Brian's durable first message.
            "durable_activity_stamp": durable_stamp,
            "route_active": self._load_attended_route_active(session_key),
            "attended_pinned": session_attended_pinned(store, session_key),
            "attended_pin_until": session_attended_pin_until(store, session_key),
            "new_session": new_session,
            "reasoning_override": reasoning_override,
            "reasoning_scope": reasoning_scope,
            "default_reasoning": default_reasoning,
            "attended_reasoning": attended_reasoning,
        }

    def _log_attended_decision(
        self, session_key, source, *, action: str, reason: str, effort: Optional[str] = None,
    ) -> None:
        platform = getattr(getattr(source, "platform", None), "value", "") if source is not None else ""
        origin = "automation" if str(platform).lower() in {
            "automation", "cron", "goal_wakeup", "kanban", "subagent", "system",
        } else "user"
        fmt, args = "attended decision: session=%s origin=%s action=%s reason=%s", [session_key, origin, action, reason]
        if effort is not None:
            fmt += " effort=%s"
            args.append(effort or "none")
        logger.info(fmt, *args)

    def _handoff_attended_state(self, session_key: Optional[str], *, conversation=None, runner=None, wait=False) -> None:
        """Publish worker-thread attended bookkeeping on the owning gateway loop."""
        def apply() -> None:
            if session_key and conversation:
                state = self._session_state(session_key)
                for name, value in conversation.items():
                    if name == "last_user_activity_at":
                        # Receipts may advance after the worker read. Compare the same instants
                        # the attended resolver accepts, rather than ordering raw store values.
                        current = _stamp_seconds(state.conversation.last_user_activity_at)
                        incoming = _stamp_seconds(value)
                        if current is not None and (incoming is None or current > incoming):
                            continue
                    setattr(state.conversation, name, value)
            if runner:
                for name, value in runner.items():
                    setattr(self, name, value)

        loop = getattr(self, "_gateway_loop", None)
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if loop is not None and loop.is_running() and current is not loop:
            if not wait:
                loop.call_soon_threadsafe(apply)
                return
            from concurrent.futures import Future
            committed = Future()

            def publish():
                try:
                    apply()
                except BaseException as exc:
                    committed.set_exception(exc)
                else:
                    committed.set_result(None)

            loop.call_soon_threadsafe(publish)
            # The worker's next routing step reads this state. Scheduling alone races it.
            committed.result(timeout=30)
        else:
            apply()

    def _load_attended_reasoning_config(self, model_name, config, *, model_role=None):
        """Resolve inherited effort for a destination from the request's profile snapshot."""
        if model_role:
            from hermes_cli.model_preset_runtime import resolve_model_ref
            with suppress(ValueError, TypeError):
                route = resolve_model_ref("@" + model_role, config=config or {})
                model_name = route.model or model_name
        loader = getattr(self, "_load_reasoning_config", lambda _model, **_kwargs: {})
        native_loader = getattr(type(self), "_load_reasoning_config", None)
        native_loader = getattr(native_loader, "__func__", native_loader)
        if getattr(loader, "__func__", loader) is native_loader:
            return loader(model_name, config=config) or {}
        return loader(model_name) or {}

    def _attended_resolution_for_session(
        self, *, source, session_key, user_config=None, override=None,
        activity_stamp=_NO_ATTENDED_ACTIVITY_STAMP, new_session=False,
        reasoning_override=_NO_ATTENDED_ACTIVITY_STAMP,
        reasoning_scope=None, default_reasoning=None, attended_reasoning=None,
        attended_pinned=_NO_ATTENDED_ACTIVITY_STAMP, attended_pin_until=None, allow_switch_back=False, assignment=None,
        durable_activity_stamp=_NO_ATTENDED_ACTIVITY_STAMP, route_active=_NO_ATTENDED_ACTIVITY_STAMP,
    ):
        """Return the one authoritative desired route decision for a gateway session.

        Loop callers pass ``durable_activity_stamp`` and ``route_active`` from the off-loop
        snapshot; the sentinel reads them here, which takes the session-store lock."""
        if not session_key:
            return None
        cfg = parse_attended_config(user_config or {})
        state = self._session_state(session_key)
        conversation = state.conversation
        assignment = assignment or {}
        model_role = assignment.get("model_role", getattr(conversation, "model_role", None))
        override = assignment.get("override", override)
        if not cfg.enabled and not model_role:
            return None
        new_session_pending = bool(new_session or getattr(conversation, "attended_new_session", False))
        new_session = new_session_pending and cfg.start_new_sessions
        stamp = conversation.last_user_activity_at
        if stamp is None or allow_switch_back:
            if activity_stamp is not _NO_ATTENDED_ACTIVITY_STAMP:
                durable_stamp = activity_stamp
            else:
                durable_stamp = self._load_attended_activity_stamp(session_key, refresh=allow_switch_back)
            if durable_stamp is not None and (stamp is None or durable_stamp > stamp):
                stamp = durable_stamp
        if new_session:
            # Routing is probed repeatedly before the first request (including image preparation).
            # Keep first-turn silence through every probe, not just the first route resolution.
            # Inbound finalization clears the marker on success, failure, and interruption.
            stamp, _ = self._ensure_attended_new_session_activity(session_key, cfg, stamp)
        self._handoff_attended_state(session_key, conversation={"last_user_activity_at": stamp})
        model_scope = assignment.get("model_scope", getattr(conversation, "model_override_scope", "session"))
        if "reasoning_override" in assignment:
            reasoning_value = assignment["reasoning_override"]
            reasoning_scope = assignment.get("reasoning_scope", "session")
        elif conversation.reasoning_override is not None:
            reasoning_value = conversation.reasoning_override
            reasoning_scope = getattr(conversation, "reasoning_override_scope", reasoning_scope)
        elif reasoning_override is _NO_ATTENDED_ACTIVITY_STAMP:
            reasoning_value = None
            with suppress(Exception):
                reasoning_value = self._session_reasoning_override(session_key)
                reasoning_scope = getattr(conversation, "reasoning_override_scope", reasoning_scope)
        else:
            reasoning_value = reasoning_override
        reasoning_scope = (getattr(conversation, "reasoning_override_scope", "session")
                           if reasoning_scope is None else reasoning_scope)
        previous_decision = conversation.last_attended_decision
        from gateway.run_session_readback import resolve_attended_snapshot
        if attended_pinned is _NO_ATTENDED_ACTIVITY_STAMP:
            from gateway.session_attended import session_attended_pin_until, session_attended_pinned
            attended_pinned = session_attended_pinned(getattr(self, "session_store", None), session_key)
            attended_pin_until = session_attended_pin_until(getattr(self, "session_store", None), session_key)
        decision = resolve_attended_snapshot(
            self, source=source, user_config=user_config, override=override,
            model_role=model_role, model_scope=model_scope, stamp=stamp, new_session=new_session,
            reasoning_value=reasoning_value, reasoning_scope=reasoning_scope,
            default_reasoning=default_reasoning, attended_reasoning=attended_reasoning,
            attended_pinned=attended_pinned, attended_pin_until=attended_pin_until,
        )
        if route_active is not _NO_ATTENDED_ACTIVITY_STAMP:
            durable_route_active = route_active
        else:
            load_route_active = getattr(self, "_load_attended_route_active", None)
            durable_route_active = load_route_active(session_key) if callable(load_route_active) else None
        from gateway.run_session_readback import attended_switch_back_pending
        switched_back = attended_switch_back_pending(
            decision, previous_decision, model_role, durable_route_active,
        )
        api_quiet = True
        awaiting_first_message = False
        if switched_back:
            idle_seconds = cfg.idle_revert_minutes * 60.0
            last_api_at = getattr(conversation, "last_api_call_at", None)
            api_quiet = last_api_at is None or time.time() - float(last_api_at) >= idle_seconds
            # A session opened by automation (a topic split, relay or wakeup) runs on the
            # attended route from a process-local stamp only. Its idle window has not started
            # until Brian sends a first genuine message, which writes the durable stamp.
            if durable_activity_stamp is _NO_ATTENDED_ACTIVITY_STAMP:
                durable_activity_stamp = self._load_attended_activity_stamp(session_key)
            awaiting_first_message = (durable_activity_stamp is None
                                      and not (attended_pinned and attended_pin_until is not None))
            if not allow_switch_back or not api_quiet or awaiting_first_message:
                # Route expiry is deferred until the request boundary. A session that has
                # called the API recently is still busy even when Brian's activity stamp is old.
                decision = previous_decision
                switched_back = False
        if new_session or switched_back:
            decision = dataclasses.replace(
                decision, new_session=new_session, switched_back=switched_back,
            )
        if decision is previous_decision:
            log_decision = getattr(self, "_log_attended_decision", None)
            if callable(log_decision):
                log_decision(
                    session_key, source, action="defer" if not allow_switch_back else "keep",
                    reason=(
                        "awaiting_first_user_message" if awaiting_first_message
                        else "session_busy" if not api_quiet else "no_api_call"
                    ),
                )
        elif not switched_back:
            self._handoff_attended_state(
                session_key,
                conversation={"last_attended_decision": decision},
                runner={"_last_attended_decision": decision},
            )
        return decision

    def _attended_route_for_session(
        self, *, source, session_key, user_config, override, last_model,
        activity_stamp=_NO_ATTENDED_ACTIVITY_STAMP, new_session=False,
        reasoning_override=_NO_ATTENDED_ACTIVITY_STAMP,
        reasoning_scope=None, default_reasoning=None, attended_reasoning=None,
        attended_pinned=_NO_ATTENDED_ACTIVITY_STAMP, attended_pin_until=None, allow_switch_back=False, assignment=None,
        durable_activity_stamp=_NO_ATTENDED_ACTIVITY_STAMP, route_active=_NO_ATTENDED_ACTIVITY_STAMP,
    ):
        """Build the detached attended route and stage its one-line notice on the session."""
        decision = self._attended_resolution_for_session(
            source=source, session_key=session_key, user_config=user_config, override=override,
            activity_stamp=activity_stamp, new_session=new_session,
            reasoning_override=reasoning_override,
            reasoning_scope=reasoning_scope, default_reasoning=default_reasoning,
            attended_reasoning=attended_reasoning, attended_pinned=attended_pinned, attended_pin_until=attended_pin_until,
            allow_switch_back=allow_switch_back, assignment=assignment,
            durable_activity_stamp=durable_activity_stamp, route_active=route_active,
        )
        if decision is None:
            return None
        conversation = self._session_state(session_key).conversation
        cfg = parse_attended_config(user_config or {})
        if decision.switched_back and cfg.notices:
            self._handoff_attended_state(
                session_key,
                conversation={
                    "pending_attended_notice": format_attended_switch_back_notice(
                        decision.model, cfg.idle_revert_minutes,
                    ),
                },
            )
        if decision.reason not in {"attended", "role"}:
            return None
        from gateway.run_session_readback import attended_runtime_route
        route = attended_runtime_route(decision)
        target_model = cfg.model or decision.model
        session_last_model = last_model or conversation.last_resolved_model or ""
        if route_active is _NO_ATTENDED_ACTIVITY_STAMP:  # loop callers pass the off-loop snapshot's value
            load_route_active = getattr(self, "_load_attended_route_active", None)
            route_active = load_route_active(session_key) if callable(load_route_active) else None
        if (
            cfg.notices
            and decision.reason != "role"
            and not decision.new_session
            # Resolution probes can carry a stale model snapshot across worker handoffs.
            # A committed attended API route is not another idle-to-attended switch.
            and route_active is not True
            and (not session_last_model or session_last_model != target_model)
        ):
            # Async resolution supplies config-derived reasoning in the detached snapshot.  Do not
            # fall back to _load_reasoning_config here: this method runs on the gateway loop.
            effort = decision.reasoning_effort or (
                (attended_reasoning if target_model == cfg.model else default_reasoning) or {}
            ).get("effort", "") or "none"
            self._handoff_attended_state(
                session_key,
                conversation={
                    "pending_attended_notice": format_attended_switch_notice(
                        target_model, effort, idle_model=decision.idle_model,
                        idle_minutes=cfg.idle_revert_minutes,
                    ),
                },
            )
        return route

    def _expire_attended_temporary_picks(
        self, session_key: str, user_config: Optional[dict],
        activity_stamp=_NO_ATTENDED_ACTIVITY_STAMP,
    ) -> None:
        """Timed model picks use their deadline, reasoning and untimed picks retain idle expiry."""
        cfg = parse_attended_config(user_config or {})
        from gateway.model_pick import model_pick_deadline
        from gateway.session_attended import session_attended_pin_until, session_attended_pinned
        deadline = model_pick_deadline(self._session_model_override(session_key))
        expired = bool(deadline and time.time() >= deadline)
        if not cfg.enabled and not expired:
            return
        state = self._session_state(session_key)
        conversation = state.conversation
        stamp = conversation.last_user_activity_at
        if stamp is None:
            if activity_stamp is not _NO_ATTENDED_ACTIVITY_STAMP:
                stamp = activity_stamp
            else:
                stamp = self._load_attended_activity_stamp(session_key)
            self._handoff_attended_state(session_key, conversation={"last_user_activity_at": stamp})
        if not expired and is_active(stamp, now=time.time(), idle_revert_minutes=cfg.idle_revert_minutes, pinned=session_attended_pinned(getattr(self, "session_store", None), session_key), pin_until=session_attended_pin_until(getattr(self, "session_store", None), session_key)):
            return
        if (expired or not deadline) and conversation.model_override is not None and conversation.model_override_scope == "temporary":
            persist = self._expire_session_model_pick
            expected = conversation.model_override
            loop = getattr(self, "_gateway_loop", None)
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                try:
                    if loop is not None and loop.is_running():
                        future = asyncio.run_coroutine_threadsafe(
                            persist(session_key, expected), loop,
                        )
                        future.result()
                    else:
                        asyncio.run(persist(session_key, expected))
                except Exception:
                    logger.debug("Failed to expire temporary attended model pick", exc_info=True)
            else:
                logger.debug("Skipping synchronous attended expiry on the gateway loop")
        if (cfg.enabled and not is_active(stamp, now=time.time(), idle_revert_minutes=cfg.idle_revert_minutes, pinned=session_attended_pinned(getattr(self, "session_store", None), session_key), pin_until=session_attended_pin_until(getattr(self, "session_store", None), session_key))
                and conversation.reasoning_override is not None and conversation.reasoning_override_scope == "temporary"):
            self._clear_attended_reasoning_override_threadsafe(session_key)

    def _clear_attended_reasoning_override_threadsafe(self, session_key: str) -> None:
        loop = getattr(self, "_gateway_loop", None)
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if loop is not None and loop.is_running() and current is not loop:
            asyncio.run_coroutine_threadsafe(
                self._clear_attended_reasoning_override_async(session_key), loop,
            ).result()
        else:
            self._set_session_reasoning_override(session_key, None)

    async def _clear_attended_reasoning_override_async(self, session_key: str) -> None:
        """Clear a temporary reasoning pick with loop-owned state and off-loop persistence."""
        state = self._session_state(session_key)
        state.conversation.reasoning_override = None
        state.conversation.reasoning_override_scope = "session"
        store = getattr(self, "session_store", None)
        if store is not None:
            await asyncio.to_thread(store.set_reasoning_override, session_key, None)

    async def _expire_attended_temporary_picks_async(
        self, session_key: str, user_config: Optional[dict],
        activity_stamp=_NO_ATTENDED_ACTIVITY_STAMP,
    ) -> None:
        """Async-loop-safe deadline and legacy idle expiry, including after restart."""
        cfg = parse_attended_config(user_config or {})
        from gateway.model_pick import model_pick_deadline
        from gateway.session_attended import session_attended_pin_until, session_attended_pinned
        pinned = await asyncio.to_thread(session_attended_pinned, getattr(self, "session_store", None), session_key)
        pin_until = await asyncio.to_thread(session_attended_pin_until, getattr(self, "session_store", None), session_key)
        deadline = model_pick_deadline(self._session_model_override(session_key))
        expired = bool(deadline and time.time() >= deadline)
        if not cfg.enabled and not expired:
            return
        state = self._session_state(session_key)
        conversation = state.conversation
        stamp = conversation.last_user_activity_at
        if stamp is None:
            if activity_stamp is _NO_ATTENDED_ACTIVITY_STAMP:
                stamp = await asyncio.to_thread(self._load_attended_activity_stamp, session_key)
            else:
                stamp = activity_stamp
            self._handoff_attended_state(session_key, conversation={"last_user_activity_at": stamp})
        if not expired and is_active(stamp, now=time.time(), idle_revert_minutes=cfg.idle_revert_minutes, pinned=pinned, pin_until=pin_until):
            return
        if (expired or not deadline) and conversation.model_override is not None and conversation.model_override_scope == "temporary":
            try:
                await self._expire_session_model_pick(session_key, conversation.model_override)
            except Exception:
                logger.debug("Failed to expire temporary attended model pick", exc_info=True)
        if (cfg.enabled and not is_active(stamp, now=time.time(), idle_revert_minutes=cfg.idle_revert_minutes, pinned=pinned, pin_until=pin_until)
                and conversation.reasoning_override is not None and conversation.reasoning_override_scope == "temporary"):
            await self._clear_attended_reasoning_override_async(session_key)

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
    def _load_session_agent_runtime(*, source, skey, user_config, config, override, last_model, attended_route=None):
        """Resolve a detached snapshot off-loop; all runner-state commits belong to the caller."""
        from gateway.run import (
            _credential_pool_for_provider, _get_channel_override, _resolve_gateway_model,
            _resolve_runtime_agent_kwargs, _resolve_runtime_agent_kwargs_for_provider,
        )
        from gateway.run_agent_cache import GatewayAgentCacheMixin

        notice = None
        model = _resolve_gateway_model(user_config)
        if override:
            override_model = override.get("model", model)
            override_runtime = {
                k: override.get(k) for k in (
                    "provider", "requested_provider", "api_key", "base_url", "api_mode",
                    "max_tokens", "credential_pool", "request_overrides", "capabilities",
                )
            }
            override_runtime["capabilities"] = dict(override_runtime["capabilities"] or {})
            if override_runtime.get("api_key"):
                if override_runtime.get("credential_pool") is None:
                    override_runtime["credential_pool"] = _credential_pool_for_provider(override.get("provider"))
                logger.debug(
                    "Session model override (fast): session=%s config_model=%s -> override_model=%s provider=%s",
                    skey or "", model, override_model, override_runtime.get("provider"),
                )
                return override_model, override_runtime, None, False
            # No api_key on the override (credentials failed to re-resolve at rehydrate): resolve them
            # for the override's own provider below, never layer it over the default provider's runtime.
            logger.debug(
                "Session model override (no api_key, fallback): session=%s config_model=%s override_model=%s",
                skey or "", model, override_model,
            )
        else:
            logger.debug("No session model override: session=%s config_model=%s", skey or "", model)

        runtime_kwargs, unavailable_override = None, None
        if override and override.get("provider"):
            try:
                runtime_kwargs = _resolve_runtime_agent_kwargs_for_provider(
                    override["provider"], target_model=override.get("model") or None)
            except Exception as exc:
                # Layering the override on the default runtime sent its model to the default provider's
                # endpoint (openai-codex on the Nous URL). Run this turn on the whole default route and say
                # so; the persisted override is kept, so the next turn retries it.
                logger.warning("Session /model override provider %s unavailable: %s", override["provider"], exc)
                unavailable_override, override = override, None
        if runtime_kwargs is None:
            runtime_kwargs = _resolve_runtime_agent_kwargs()
        # Private notice metadata must never reach an ``AIAgent(**runtime_kwargs)`` spread; the turn
        # runner surfaces it through the agent's one-shot fallback notice (#74349).
        notice = runtime_kwargs.pop("_fallback_notice", None)
        runtime_model = runtime_kwargs.pop("model", None)
        if runtime_model:
            logger.info("Runtime provider supplied explicit model override: %s -> %s", model, runtime_model)
            model = runtime_model
        if unavailable_override and not notice:
            from hermes_cli.fallback_config import pre_agent_fallback_notice
            notice = pre_agent_fallback_notice(
                unavailable_override["provider"], unavailable_override.get("model"), runtime_kwargs.get("provider"), model)

        if config and source is not None:
            ch = _get_channel_override(
                config, source.platform, str(source.chat_id) if source.chat_id else "",
                thread_id=str(source.thread_id) if getattr(source, "thread_id", None) else None,
                parent_id=str(source.parent_chat_id) if getattr(source, "parent_chat_id", None) else None,
            )
            if ch:
                if ch.model:
                    model = ch.model
                if ch.provider:
                    runtime_kwargs = _resolve_runtime_agent_kwargs_for_provider(ch.provider, target_model=model or None)
                    ch_runtime_model = runtime_kwargs.pop("model", None)
                    # Adopt the provider's bundled model only when the override named none.
                    if ch_runtime_model and not ch.model:
                        model = ch_runtime_model

        if attended_route and attended_route.get("active"):
            attended_model = attended_route.get("model") or model
            attended_provider = attended_route.get("provider") or ""
            if attended_provider:
                try:
                    runtime_kwargs = _resolve_runtime_agent_kwargs_for_provider(
                        attended_provider, target_model=attended_model or None,
                    )
                    provider_model = runtime_kwargs.pop("model", None)
                    if provider_model and not attended_route.get("model"):
                        attended_model = provider_model
                    model = attended_model
                except Exception as exc:
                    logger.warning("Attended route %s unavailable: %s", attended_provider, exc)
                    notice = ContextNotice(f"⚠️ Attended model unavailable; staying on {model}.")
            else:
                model = attended_model

        if override and skey:
            model, runtime_kwargs = GatewayAgentCacheMixin._apply_model_override(override, model, runtime_kwargs)

        # Provider resolved but no model.default (`hermes auth add` without `hermes model`): use the
        # provider's first catalog model.
        if not model and runtime_kwargs.get("provider"):
            with suppress(Exception):
                from hermes_cli.models import get_default_model_for_provider
                model = get_default_model_for_provider(runtime_kwargs["provider"])
                if model:
                    logger.info(
                        "No model configured — defaulting to %s for provider %s", model, runtime_kwargs["provider"],
                    )

        # Final safety net: an empty model (transient config-cache miss) makes every API call 400 and
        # the session goes silent — reuse the last model resolved for this session, else process-wide.
        cache_model = bool(model)
        if not model:
            if last_model:
                logger.warning(
                    "Empty model resolved for session=%s — recovering "
                    "last-known-good model %s (config read likely returned "
                    "empty; see #35314)", skey or "", last_model,
                )
                model = last_model

        return model, runtime_kwargs, notice, cache_model

    async def _resolve_session_agent_runtime_async(
        self, *, source: Optional[SessionSource] = None, session_key: Optional[str] = None,
        user_config: Optional[dict] = None,
    ) -> tuple[str, dict]:
        """Off-loop reads/credentials, on-loop snapshots and compare-and-set commits."""
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
            resolved = await asyncio.to_thread(
                self._load_session_agent_runtime, source=source, skey=skey, user_config=user_config,
                config=getattr(self, "config", None), override=override, last_model=last_model,
                attended_route=attended_route,
            )
            # Credential resolution can also yield to /model or a boundary. Retry
            # against live memory, not disk, so a pending durable clear cannot revive it.
            if not skey or self._session_model_override_epoch(skey) == epoch:
                return self._commit_session_agent_runtime(skey, resolved)





    def _event_thread_metadata(self, event, source):
        """Thread metadata for a send that replies to ``event`` on ``source``."""
        return self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))

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





























































    async def _hmwa_stop_typing_for_turn(self, event, source):
        """Stop the typing indicator (never raises), with the delivery path's thread/workspace routing
        metadata (Slack AI status is thread-scoped), on this turn's own message, not the chat's latest."""
        from gateway.platforms.helpers import typing_metadata_for_turn
        with suppress(Exception):
            _typing_adapter = self._delivery_adapter_for(source)
            _kind = type(_typing_adapter)
            if _typing_adapter and callable(getattr(_kind, "_stop_typing_with_metadata", None)):
                await _typing_adapter._stop_typing_with_metadata(source.chat_id, typing_metadata_for_turn(_typing_adapter,
                    source.chat_id, self._reply_anchor_for_event(event), self._event_thread_metadata(event, source)))
            elif _typing_adapter and callable(getattr(_kind, "stop_typing", None)):
                await _typing_adapter.stop_typing(source.chat_id)



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
            "inbound message: platform=%s user=%s chat=%s msg=%r reply_to_id=%s reply_to_text=%r",
            _platform_name, source.user_name or source.user_id or "unknown",
            source.chat_id or "unknown", (event.text or "")[:80].replace("\n", " "),
            getattr(event, "reply_to_message_id", None),
            (getattr(event, "reply_to_text", None) or "")[:80].replace("\n", " "),
        )

        if not await self._prepare_loop_wake_execution(event, source):
            self._log_loop_wake_suppressed(event, "superseded before session resolution")
            return
        _owned_loop_fence = getattr(event, "_gateway_loop_execution_fence", None)
        if _owned_loop_fence is not None:
            run_generation = _owned_loop_fence[2]

        resolved = await self._hmwa_resolve_session(event, source)
        if resolved is None:
            return
        source, session_entry, session_key = resolved
        event._hermes_attended_session_key = session_key
        if not getattr(event, "_attended_activity_marked", False):
            mark_activity = getattr(self, "_mark_attended_user_activity", None)
            if callable(mark_activity):
                activity_result = mark_activity(event, source, session_key)
                if inspect.isawaitable(activity_result):
                    await activity_result
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
        if not await self._prepare_loop_wake_execution(event, source):
            self._log_loop_wake_suppressed(event, "superseded before turn preparation")
            return
        from gateway.run_checkins import prepare_checkin
        if not await prepare_checkin(self, event, session_entry):
            return
        prepared, _session_env_tokens = await self._hmwa_prepare_turn(
            event, source, session_entry, session_key, _quick_key, run_generation,
        )
        if not isinstance(prepared, self._PreparedTurn):
            return prepared
        history, message_text = prepared.history, prepared.message_text

        try:
            if not await self._prepare_loop_wake_execution(event, source):
                self._log_loop_wake_suppressed(event, "superseded during turn preparation")
                return
            hook_ctx = {
                "platform": source.platform.value if source.platform else "",
                "user_id": source.user_id,
                "chat_id": source.chat_id or "",
                "thread_id": str(source.thread_id) if getattr(source, "thread_id", None) else "",
                "chat_type": getattr(source, "chat_type", "") or "",
                "session_id": session_entry.session_id,
                "message": message_text[:500],
            }
            await self.hooks.emit("agent:start", hook_ctx)

            # Capture the launch session id so post-run compression publication is identity-guarded
            # (a /new may move session_entry.session_id while the old run is still unwinding).
            from gateway.run_heartbeat_acceptance import heartbeat_owner_is_current
            if not heartbeat_owner_is_current(self, event, session_key):
                return
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
            # Internal events reuse the last human turn's channel inputs (see _pinned_channel_inputs).
            _turn_channel_prompt, _turn_source = self._pinned_channel_inputs(
                session_key, event.channel_prompt, source,
                internal=event.internal or getattr(event, "inherit_channel_pin", False),
            )
            if not event.internal:
                # Persist the coherent context+channel pair before execution: a crash during the
                # human turn may be followed by an internal startup-resume on the next process.
                await self._persist_prompt_pins(session_key, _run_start_session_id)
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
            from gateway.run_checkins import silence_fallback
            agent_result = await silence_fallback(self, event, session_entry, agent_result)
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
                if agent_result.get("queued_chain_loop_tick"):
                    # The latest loop wake the chain ran owns the loop completion.
                    event._post_turn_loop_tick = agent_result["queued_chain_loop_tick"]

            await self._hmwa_stop_typing_for_turn(event, source)

            if not self._is_session_run_current(_quick_key, run_generation):
                self._hmwa_discard_stale_result(source, _quick_key, run_generation)
                return None

            response, _intentional_silence, agent_messages = await self._hmwa_shape_agent_response(
                agent_result, source, history, session_entry, session_key,
                _quick_key, run_generation, _run_start_session_id, _platform_name, _msg_start_time,
                persist_user_display_kind=prepared.persist_user_display_kind,
                reply_expected=event.reply_expected,
            )
            response = self._usage_window_chat_wording(agent_result, self._hmwa_prepend_reasoning(agent_result, response, source, _intentional_silence))
            from gateway.response_filters import strip_trailing_loop_complete_marker
            response = strip_trailing_loop_complete_marker(response)
            _footer_line = self._hmwa_runtime_footer_line(agent_result, source, _turn_seconds)
            # Streaming already delivered the body: the footer goes out as a trailing send instead.
            if _footer_line and response and not agent_result.get("already_sent") and not _intentional_silence:
                response = f"{response}\n\n{_footer_line}"
            await self._hmwa_post_turn_hooks(hook_ctx, agent_result, response)

            agent_failed_early, hidden_reasoning_incomplete, is_context_overflow_failure = (
                self._hmwa_classify_turn_failure(agent_result, history, session_entry)
            )
            if agent_failed_early and not is_context_overflow_failure and automatic_notices_allowed(source):
                response = self._hmwa_add_failed_turn_notice(response, self._hmwa_failed_turn_notice(agent_result))
            response, session_entry = await self._hmwa_compression_exhaustion_reset(
                agent_result, response, session_entry, session_key, source,
            )
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
            return self._format_session_info()

    def _format_session_info(self) -> str:
        """Model / provider / context-length / endpoint block so users can spot bad context detection."""
        from gateway.run import _resolve_gateway_model_context
        resolved = _resolve_gateway_model_context()
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
        if override and isinstance(override, list):
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



    def _mcp_reload_refresh_cached_agents(self, multiplex: bool, profile) -> None:
        """Refresh cached agents so existing sessions see new MCP tools on their next turn without
        a history-destroying ``/new``. Each agent keeps its build-time toolset selection EXACTLY: a
        session built with restricted enabled_toolsets (e.g. ["safe"]) must NOT silently gain tools."""
        try:
            from tools.mcp_tool_agent import refresh_agent_mcp_tools
            _cache = getattr(self, "_agent_cache", None)
            _cache_lock = getattr(self, "_agent_cache_lock", None)
            if _cache_lock is None or not _cache:
                return
            # Multiplex: only this profile's sessions (another profile's agent would get this registry).
            _ns_prefix = _session_key_namespace(profile) + ":" if multiplex else None
            with _cache_lock:
                for _sess_key, _entry in list(_cache.items()):
                    if _ns_prefix and not str(_sess_key).startswith(_ns_prefix):
                        continue
                    _agent = _entry[0] if isinstance(_entry, tuple) else _entry
                    if _agent is not None:
                        refresh_agent_mcp_tools(_agent, quiet_mode=True)
        except Exception as _exc:
            logger.debug("Failed to update cached agent tools after MCP reload: %s", _exc)

    async def _execute_mcp_reload(self, event: MessageEvent) -> str:
        """Disconnect, reconnect, and notify MCP tool changes (shared by button / text / no-confirm paths).

        Under multiplex the reload runs inside the requesting profile's runtime scope (entered here
        when the caller did not) and only that profile's servers are torn down and rediscovered.

        See #95518.
        """
        from gateway.run import _profile_runtime_scope
        multiplex = bool(getattr(self.config, "multiplex_profiles", False))
        if multiplex and not get_hermes_home_override():
            profile_home = self._resolve_profile_home_for_source(event.source)
            with _profile_runtime_scope(Path(profile_home)):
                return await self._execute_mcp_reload(event)
        try:
            from tools.mcp_tool_lifecycle import shutdown_mcp_servers
            from tools.mcp_tool_discovery import discover_mcp_tools
            from tools.mcp_tool import _servers, _lock, _server_visible_in_scope
            from tools.mcp_tool_agent import reprobe_tool_availability
            from tools.mcp_tool_scope import _key_name
            from tools.registry import registry

            reload_scope = registry.current_scope_key() if multiplex else None

            def _scoped_server_names() -> set:
                with _lock:
                    return {
                        _key_name(key) for key in _servers
                        if _server_visible_in_scope(key, reload_scope)
                    }

            old_servers = _scoped_server_names()
            await self._run_in_executor_with_context(lambda: shutdown_mcp_servers(scope=reload_scope))
            # Explicit reload also re-probes tool availability (check_fn).
            reprobe_tool_availability()
            # Reconnect by discovering tools (reads config.yaml fresh). A chat command cannot finish
            # a browser OAuth flow either: an expired token parks with a `hermes mcp login` hint.
            from tools.mcp_oauth import suppress_interactive_oauth
            with suppress_interactive_oauth():
                new_tools = await self._run_in_executor_with_context(discover_mcp_tools)

            connected_servers = _scoped_server_names()
            if reload_scope is not None:
                from tools.mcp_tool import _mcp_tool_server_names
                with _lock:
                    new_tools = [n for n in new_tools if _mcp_tool_server_names.get(n) in connected_servers]
            # (label, i18n key, names); i18n lines list reconnected first, the injected note added first.
            changes = (
                ("Reconnected", "gateway.reload_mcp.reconnected", connected_servers & old_servers),
                ("Added", "gateway.reload_mcp.added", connected_servers - old_servers),
                ("Removed", "gateway.reload_mcp.removed", old_servers - connected_servers),
            )
            lines = [t("gateway.reload_mcp.header")] + [
                t(key, names=", ".join(sorted(names))) for _label, key, names in changes if names
            ]
            from tools.mcp_tool_discovery import mcp_servers_not_connected
            failed = await self._run_in_executor_with_context(mcp_servers_not_connected)
            if not connected_servers:
                lines.append(t("gateway.reload_mcp.none_connected"))
            else:
                lines.append(t("gateway.reload_mcp.tools_available", tools=len(new_tools), servers=len(connected_servers)))
            if failed:  # a configured server that did not connect is named, never passed over as success
                lines.append(t("gateway.reload_mcp.failed", error=f"did not connect: {', '.join(failed)}"))

            self._mcp_reload_refresh_cached_agents(multiplex, event.source.profile)

            # Append a note at the END of the history (preserves the prompt-cache prefix).
            change_parts = [
                f"{label} servers: {', '.join(sorted(names))}"
                for label, _key, names in (changes[1], changes[2], changes[0]) if names
            ]
            tool_summary = f"{len(new_tools)} MCP tool(s) now available" if new_tools else "No MCP tools available"
            change_detail = ". ".join(change_parts) + ". " if change_parts else ""
            reload_msg = {
                "role": "user",
                "content": f"[IMPORTANT: MCP servers have been reloaded. {change_detail}{tool_summary}. The tool list for this conversation has been updated accordingly.]",
            }
            with suppress(Exception):  # Best-effort; don't fail the reload over a transcript write
                session_entry = await self.async_session_store.get_or_create_session(event.source)
                await self.async_session_store.append_to_transcript(session_entry.session_id, reload_msg)

            return "\n".join(lines)

        except Exception as e:
            logger.warning("MCP reload failed: %s", e)
            return t("gateway.reload_mcp.failed", error=e)

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
        _scfg = getattr(getattr(self, "config", None), "streaming", None)
        # #60671 — streaming TTS consumer is created on the outer event-loop thread before run_sync
        # launches.  run_sync only reads it via ``streaming_tts_consumer_holder[0]`` for delta callback
        # wiring.
        if _scfg is None:
            from gateway.config import StreamingConfig
            _scfg = StreamingConfig()
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
                        logger.warning("Proxy error (%d) from %s: %s", resp.status, proxy_url, error_text[:500])
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
                            "(%d chars received)", proxy_url, len(full_response),
                        )
                        if not full_response:
                            return self._proxy_error_result(t("gateway.proxy.closed_early"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Proxy connection error to %s: %s", proxy_url, e)
            if not full_response:
                return self._proxy_error_result(t("gateway.proxy.connection_error", error=e))
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
            proxy_url, (session_id or "")[:20], _elapsed, len(full_response),
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
        # type: a fake adapter without it means "can't delete", not a crash).
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
            run_generation=run_generation, _cleanup_progress=_cleanup_progress,
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
        """Peek the adapter's pending event, transcribe voice, then signal the agent + abort streaming TTS.

        Peek WITHOUT consuming: the event must stay for the post-run ``_dequeue_pending_event()``
        (popping races the agent finishing). Transcribe BEFORE signaling so voice interrupts carry
        the real transcript."""
        from gateway.run import _build_media_placeholder
        _peek_event = adapter._pending_messages.get(session_key)
        pending_text = None
        if _peek_event is not None:
            pending_text = _peek_event.text or ""
            if self._pending_event_audio_paths(_peek_event):
                pending_text, _ = await self._transcribe_and_echo_pending_voice(
                    _peek_event, adapter, source, pending_text, log_context=log_context,
                    metadata={"thread_id": source.thread_id} if source.thread_id else None,
                )
            elif not pending_text and (getattr(_peek_event, "media_urls", None) or []):
                pending_text = _build_media_placeholder(_peek_event)
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
                if not _warning_fired and worker.agent_warning is not None and _idle_secs >= worker.agent_warning:
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
            from gateway.session_policy import policy_for_source
            policy = policy_for_source(self, turn_ctx.source)
            _cfg_model = self._fallback_baseline_model(
                session_key, turn_ctx.source, _agent,
                policy_model=policy.current_model(active_authority(self)) if policy and policy.model else None,
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
            silence_kind = None if getattr(turn_ctx, "silence_retry", False) else turn_ctx.persist_user_display_kind
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
                _text_delivered = await self._deliver_queued_first_response(
                    first_response, source=turn_ctx.source, adapter=adapter,
                    metadata=turn_ctx._status_thread_metadata, event_message_id=turn_ctx.event_message_id,
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
        logger.debug("Processing pending message: '%s...'", pending[:40])

        # Clear the interrupt event so the recursive _run_agent isn't re-interrupted (infinite loop).
        _active = getattr(adapter, "_active_sessions", None) if adapter else None
        if _active and session_key and session_key in _active:
            _active[session_key].clear()

        # Cap recursion depth (user keeps sending while the agent keeps failing).
        # (#816)
        if _interrupt_depth >= self._MAX_INTERRUPT_DEPTH:
            logger.warning(
                "Interrupt recursion depth %d reached for session %s — "
                "queueing message instead of recursing.", _interrupt_depth, session_key,
            )
            adapter = self._delivery_adapter_for(source)
            if adapter and session_key and hasattr(adapter, "_pending_messages"):
                self._requeue_at_depth_cap(session_key, source, pending_event, pending)
                if pending_event is not None:
                    pending_event._gateway_accepted = True
            elif adapter and hasattr(adapter, 'queue_message'):
                # Legacy adapters keep the fork's event metadata contract.
                try:
                    adapter.queue_message(
                        session_key, pending, reply_expected=turn_ctx.reply_expected,
                        internal=turn_ctx.internal, metadata=dict(turn_ctx.event_metadata or {}),
                    )
                except TypeError:
                    adapter.queue_message(session_key, pending)
            return turn_ctx.result_holder[0] or {"final_response": response, "messages": history}

        # Interrupted: discard the response ("Operation interrupted." is noise).
        if not result.get("interrupted"):
            delivered = await self._run_agent_deliver_first_response(
                turn_ctx, adapter, response, result, stream_task,
            )
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
                        self._session_state(session_key).conversation.queued_events.insert(0, existing)
                    adapter._pending_messages[session_key] = deferred
                logger.warning("Queued follow-up deferred after failed first delivery for %s", session_key)
                return result or {"final_response": response, "messages": history}

            owner = turn_ctx._post_delivery_adapter or adapter
            callback = self._pop_post_delivery_callback(owner, session_key, turn_ctx.run_generation)
            if callable(callback):
                with suppress(Exception):
                    callback_result = callback()
                    if inspect.isawaitable(callback_result):
                        await callback_result
            if pending_event is not None:
                # The queued follow-up's turn starts now: its busy ack has done its job.
                from gateway.platforms.base import release_temporary_notices
                await release_temporary_notices(pending_event)

        updated_history = result.get("messages", history)
        next_source, next_message, next_session_key = source, pending, session_key
        # message_type is carried into the recursive call so queued voice turns can stream TTS.
        next_message_id = next_channel_prompt = next_message_type = None
        # The raw inbound id keys the delivery-ledger obligation for the follow-up's own final send,
        # distinct from the reply anchor above (None in forum topics). Carry it or two chained
        # topic turns with the same text would collide on one obligation id (queued-final-ledger).
        next_inbound_id = None
        # Queued Discord turns carry the same routing note as first turns; persist the authored text.
        next_persist_message = None
        next_display_kind = display_kind_for_event(pending_event)
        next_reply_expected = pending_event.reply_expected if pending_event is not None else None
        if (
            pending_event is not None
            and isinstance(getattr(pending_event, "metadata", None), dict)
            and pending_event.metadata.get("goal_continuation")
            and next_reply_expected is not True
        ):
            next_reply_expected = False
        # See #60671.
        if pending_event is not None:
            next_source = getattr(pending_event, "source", None) or source
            # A strict event queued before /new or /resume rebound this key belongs to the session
            # it was pinned to, not to whichever conversation this drain is now running.
            if not await GatewayTurnMixin._strict_session_pin_holds(self, pending_event, session_id):
                return result
            from gateway.platforms.event import is_goal_continuation_event
            if is_goal_continuation_event(pending_event):
                from hermes_cli.session_controls import refresh_goal_continuation
                if await self._run_in_executor_with_context(
                    refresh_goal_continuation, pending_event, session_id,
                ) is None:
                    logger.info("Discarding stale goal continuation for session %s", session_key or "?")
                    return result
            from hermes_cli.session_controls import LOOP_WAKE_METADATA_KEY, loop_wake_is_current
            if (getattr(pending_event, "metadata", None) or {}).get(LOOP_WAKE_METADATA_KEY) and not await (
                    self._run_in_executor_with_context(
                        loop_wake_is_current, pending_event.metadata, session_id)):
                logger.info("Discarding superseded loop wake for session %s", session_key or "?")
                return result
            # Resolve the follow-up's session key BEFORE preparing the inbound text: native image
            # paths are buffered under the key given and consumed under next_session_key.
            try:
                next_session_key = self._session_key_for_source(next_source)
            except Exception:
                logger.debug(
                    "Queued follow-up session-key resolution failed; reusing %s",
                    session_key or "?", exc_info=True,
                )
            next_message = await self._prepare_profile_scoped_inbound_message_text(
                event=pending_event, source=next_source, history=updated_history, session_key=next_session_key,
            )
            if next_message is None:
                return result
            from gateway.run_inbound import strip_discord_triggering_note
            next_persist_message = strip_discord_triggering_note(pending_event, next_message)
            next_message_id = self._reply_anchor_for_event(pending_event)
            next_inbound_id = str(pending_event.message_id) if getattr(pending_event, "message_id", None) else None
            next_channel_prompt, next_source = self._pinned_channel_inputs(
                next_session_key, pending_event.channel_prompt, next_source,
                internal=pending_event.internal or getattr(pending_event, "inherit_channel_pin", False),
            )
            if not pending_event.internal:
                # A drained human turn re-pins its channel inputs; make them durable like a first turn.
                await self._persist_prompt_pins(next_session_key, session_id)
            next_message_type = getattr(pending_event, "message_type", None)
        else:
            # Event-less interrupt/steer follow-ups continue the effective prompt
            # of the turn they are recursively following.
            next_channel_prompt = turn_ctx.channel_prompt

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
            if pending_event is not None and GatewayTurnMixin._is_gateway_wake(pending_event):
                next_message = await GatewayTurnMixin._internal_wake_input_at_execution(
                    self, pending_event, session_id, next_message)
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
        # The deepest queued loop wake wins (inner recursions fill this first): it is the latest
        # tick the chain ran, so the chain's single loop completion settles exactly that tick.
        _pending_loop_tick = self._post_turn_goal_identity(pending_event).get("loop_tick")
        if isinstance(merged, dict) and _pending_loop_tick and not merged.get("queued_chain_loop_tick"):
            merged = {**merged, "queued_chain_loop_tick": _pending_loop_tick}
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
            logger.info(
                "Suppressing normal final send for session %s: final delivery already confirmed (streamed=%s previewed=%s content_delivered=%s).",
                _sk, _streamed, _previewed, _content_delivered,
            )
            response["already_sent"] = True
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
                await self._run_agent_edit_streamed_message(
                    _sc, source, response, _final, _sk=_sk,
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
                await self._run_agent_edit_streamed_message(
                    _sc, source, response, response["final_response"], _sk=_sk,
                    ok=("Edited streamed message %s for session %s to include plugin-transformed content.", _sc.message_id, _sk),
                    fail_result="Transformed-final edit failed for session %s (%s); sending transformed response via normal final send.",
                    fail_exc="Failed to edit streamed message for session %s: %s",
                )
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

    def _run_agent_schedule_bubble_cleanup(self, response: Any, _cleanup_adapter: Any, turn_ctx: TurnContext) -> None:
        """Register turn-local deletion, awaited after confirmed final delivery."""
        ids, key = turn_ctx._cleanup_msg_ids, turn_ctx.session_key
        delivery = turn_ctx._status_delivery
        if delivery is not None:
            delivery.closed = True
        if turn_ctx._bubble_cleanup_registered:
            return
        if not (
            turn_ctx._cleanup_progress and _cleanup_adapter is not None and (ids or delivery is not None) and key
            and isinstance(response, dict) and not response.get("failed")
            and hasattr(_cleanup_adapter, "register_post_delivery_callback")
        ):
            if turn_ctx._cleanup_progress and ids and isinstance(response, dict) and response.get("failed"):
                logger.info("Temp bubble cleanup skipped for session %s: failed run", key)
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
                except Exception as error:
                    failures.extend(f"{mid}:{type(error).__name__}" for mid in owned)
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
        turn_ctx._status_adapter = self._delivery_adapter_for(source)
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
        _notify_adapter = self._delivery_adapter_for(source)
        if not _notify_adapter:
            return
        _heartbeat_msg_id: Optional[str] = None
        while True:
            await asyncio.sleep(_NOTIFY_INTERVAL)
            if not self._should_emit_long_running_notification(
                session_key, agent_holder[0], _executor_task_holder[0]
            ):
                break
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
                            turn_ctx._cleanup_msg_ids.append(_heartbeat_msg_id)
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
            self._run_agent_schedule_bubble_cleanup(response, _cleanup_adapter, turn_ctx)
            # A queued successor already supplies a fresh turn; drain it before considering recovery.
            pending_event, pending = await self._run_agent_drain_pending(result, adapter, source, session_key)
            pending_event, pending = defer_review_followup(self, turn_ctx, adapter, pending_event, pending)
            if pending_event or pending:
                return await self._run_agent_queued_followup(
                    turn_ctx, adapter, pending, pending_event, response, result, stream_task,
                )
            from gateway.run_turn_silence_recovery import silence_recovery_due
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
