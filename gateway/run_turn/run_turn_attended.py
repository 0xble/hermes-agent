'''Attended-route methods split from the Gateway turn facade.'''

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from contextlib import suppress
from typing import Optional

from gateway.attended_model import (
    _stamp_seconds,
    format_attended_switch_back_notice,
    format_attended_switch_notice,
    is_active,
    parse_attended_config,
)
from gateway.run_turn import _NO_ATTENDED_ACTIVITY_STAMP

logger = logging.getLogger("gateway.run")

class GatewayAttendedMixin:
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
            from hermes_cli.model_alias_runtime import resolve_model_alias
            with suppress(ValueError, TypeError):
                route = resolve_model_alias(model_role, config=config or {}, required=True)
                model_name = route.model or model_name
                if route.reasoning_effort != "":
                    from hermes_constants import parse_reasoning_effort
                    return parse_reasoning_effort(route.reasoning_effort)
        loader = getattr(self, "_load_reasoning_config", lambda _model, **_kwargs: {})
        native_loader = getattr(type(self), "_load_reasoning_config", None)
        native_loader = getattr(native_loader, "__func__", native_loader)
        if getattr(loader, "__func__", loader) is native_loader:
            return loader(model_name, config=config) or {}
        return loader(model_name) or {}

    def _attended_reasoning_inputs(
        self, session_key, conversation, assignment, reasoning_override, reasoning_scope,
    ):
        if "reasoning_override" in assignment:
            return assignment["reasoning_override"], assignment.get("reasoning_scope", "session")
        if conversation.reasoning_override is not None:
            return conversation.reasoning_override, getattr(conversation, "reasoning_override_scope", reasoning_scope)
        if reasoning_override is not _NO_ATTENDED_ACTIVITY_STAMP:
            return reasoning_override, reasoning_scope
        value = None
        with suppress(Exception):
            value = self._session_reasoning_override(session_key)  # type: ignore[attr-defined]
            reasoning_scope = getattr(conversation, "reasoning_override_scope", reasoning_scope)
        return value, reasoning_scope

    def _attended_resolution_for_session(
        self, *, source, session_key, user_config=None, override=None,
        activity_stamp=_NO_ATTENDED_ACTIVITY_STAMP, new_session=False,
        reasoning_override=_NO_ATTENDED_ACTIVITY_STAMP,
        reasoning_scope=None, default_reasoning=None, attended_reasoning=None,
        attended_pinned=_NO_ATTENDED_ACTIVITY_STAMP, attended_pin_until=None, allow_switch_back=False, assignment=None,
        durable_activity_stamp=_NO_ATTENDED_ACTIVITY_STAMP, route_active=_NO_ATTENDED_ACTIVITY_STAMP, record_notice=True,
    ):
        """Return the one authoritative desired route decision for a gateway session.

        Loop callers pass ``durable_activity_stamp`` and ``route_active`` from the off-loop
        snapshot; the sentinel reads them here, which takes the session-store lock."""
        if not session_key:
            return None
        cfg = parse_attended_config(user_config or {})
        state = self._peek_session_state(session_key)
        conversation = state.conversation if state is not None else None
        assignment = assignment or {}
        model_role = assignment.get("model_role", getattr(conversation, "model_role", None))
        override = assignment.get("override", override)
        if not cfg.enabled and not model_role:
            return None
        state = state if state is not None or not record_notice else self._session_state(session_key)
        from gateway.session_state import ConversationState
        conversation = state.conversation if state is not None else ConversationState()
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
        if new_session and record_notice:
            # Routing is probed repeatedly before the first request (including image preparation).
            # Keep first-turn silence through every probe, not just the first route resolution.
            # Inbound finalization clears the marker on success, failure, and interruption.
            stamp, _ = self._ensure_attended_new_session_activity(session_key, cfg, stamp)
        if record_notice:
            self._handoff_attended_state(session_key, conversation={"last_user_activity_at": stamp})
        model_scope = assignment.get("model_scope", getattr(conversation, "model_override_scope", "session"))
        reasoning_value, reasoning_scope = self._attended_reasoning_inputs(
            session_key, conversation, assignment, reasoning_override, reasoning_scope,
        )
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
        if not record_notice:
            return decision
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
        durable_activity_stamp=_NO_ATTENDED_ACTIVITY_STAMP, route_active=_NO_ATTENDED_ACTIVITY_STAMP, record_notice=True,
    ):
        """Build the detached attended route and stage its one-line notice on the session."""
        decision = self._attended_resolution_for_session(
            source=source, session_key=session_key, user_config=user_config, override=override,
            activity_stamp=activity_stamp, new_session=new_session,
            reasoning_override=reasoning_override,
            reasoning_scope=reasoning_scope, default_reasoning=default_reasoning,
            attended_reasoning=attended_reasoning, attended_pinned=attended_pinned, attended_pin_until=attended_pin_until,
            allow_switch_back=allow_switch_back, assignment=assignment,
            durable_activity_stamp=durable_activity_stamp, route_active=route_active, record_notice=record_notice,
        )
        if decision is None:
            return None
        if not record_notice:
            from gateway.run_session_readback import attended_runtime_route
            return attended_runtime_route(decision) if decision.reason in {"attended", "role"} else None
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
