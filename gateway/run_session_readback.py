"""Detached routing snapshots shared by turn setup and read-only settings inspection."""
from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import replace

from gateway.attended_model import is_active, parse_attended_config, resolve_attended_model
from gateway.model_pick import model_pick_deadline
from gateway.session import sanitize_reasoning_override
from gateway.session_attended import session_attended_pin_until, session_attended_pinned
from gateway.session_state import ConversationState, session_opening_pending


def resolve_attended_snapshot(
    runner, *, source, user_config, override, model_role, model_scope, stamp, new_session,
    reasoning_value, reasoning_scope, default_reasoning=None, attended_reasoning=None, attended_pinned=False, attended_pin_until=None,
):
    """Project detached inputs into the existing pure precedence resolver, without a commit."""
    from gateway.run import _get_channel_override, _resolve_gateway_model

    cfg = parse_attended_config(user_config or {})
    if not cfg.enabled and not model_role:
        return None
    channel_model = channel_provider = ""
    gateway_config = getattr(runner, "config", None)
    if source is not None and gateway_config is not None:
        with suppress(Exception):
            ch = _get_channel_override(
                gateway_config, source.platform, str(source.chat_id) if source.chat_id else "",
                thread_id=str(source.thread_id) if getattr(source, "thread_id", None) else None,
                parent_id=str(source.parent_chat_id) if getattr(source, "parent_chat_id", None) else None,
            )
            channel_model = getattr(ch, "model", "") or ""
            channel_provider = getattr(ch, "provider", "") or ""
    default_model = _resolve_gateway_model(user_config)
    model_value = (override or {}).get("model")
    model_provider = (override or {}).get("provider")
    # A held role may differ even when setup supplied a default baseline.
    if default_reasoning is None or (model_role and not model_value):
        default_reasoning = {}
        with suppress(Exception):
            default_reasoning = runner._load_attended_reasoning_config(
                str(model_value or channel_model or default_model or ""), user_config,
                model_role=model_role if not model_value else None,
            )
    if attended_reasoning is None:
        attended_reasoning = {}
        with suppress(Exception):
            if cfg.model:
                attended_reasoning = runner._load_attended_reasoning_config(cfg.model, user_config)
    return resolve_attended_model(
        {
            "model_override": model_value if model_scope not in ("session", "temporary", "once") else None,
            "once_model": model_value if model_scope == "once" else None,
            "session_model": model_value if model_scope == "session" else None,
            "temporary_model": model_value if model_scope == "temporary" else None,
            "model_override_provider": model_provider,
            "default_model": default_model,
            "default_provider": (user_config or {}).get("model", {}).get("provider", "")
            if isinstance((user_config or {}).get("model"), dict) else "",
            "channel_model": channel_model,
            "channel_provider": channel_provider,
            "last_user_activity_at": stamp,
            "attended_pinned": attended_pinned,
            "attended_pin_until": attended_pin_until,
            "new_session": new_session,
            "model_role": model_role,
            "default_reasoning": default_reasoning,
            "explicit_model_reasoning": default_reasoning,
            "attended_reasoning": attended_reasoning,
            "session_reasoning": reasoning_value if reasoning_scope == "session" else None,
            "temporary_reasoning": reasoning_value if reasoning_scope == "temporary" else None,
        },
        time.time(), user_config or {},
    )


def attended_switch_back_pending(decision, previous_decision, model_role, durable_route_active):
    """Only automatic attended expiry waits for a quiet API boundary, not a missing role."""
    return bool(
        decision.reason == "default"
        and not model_role
        and (
            (
                previous_decision is not None
                and previous_decision.reason == "attended"
                and previous_decision.active
                and previous_decision.idle_model
                and previous_decision.idle_model != previous_decision.model
            )
            or durable_route_active is True
        )
    )


def attended_runtime_route(decision):
    """The credential loader only needs the selected attended or held-role route."""
    if decision is None or decision.reason not in {"attended", "role"}:
        return None
    return {
        "active": True, "model": decision.model, "provider": decision.provider,
        "reasoning_effort": decision.reasoning_effort, "decision_key": decision.decision_key,
        "role": decision.role,
    }


def _load_session_settings(runner, *, source, session_key, user_config, conversation, last_model):
    """Read durable inputs and credentials off-loop, changing only the detached conversation."""
    store = runner.session_store
    if conversation.model_override is None:
        conversation.model_override, conversation.model_override_scope = runner._load_session_model_override(session_key)
    if conversation.model_role is None:
        conversation.model_role = store.get_model_role(session_key)
    if conversation.reasoning_override is None:
        conversation.reasoning_override = sanitize_reasoning_override(store.get_reasoning_override(session_key))
        conversation.reasoning_override_scope = store.get_reasoning_override_scope(session_key)
    stamp = conversation.last_user_activity_at
    if stamp is None:
        stamp = runner._load_attended_activity_stamp(session_key)
    cfg = parse_attended_config(user_config or {})
    now = time.time()
    pinned = session_attended_pinned(store, session_key)
    pin_until = session_attended_pin_until(store, session_key)
    active = is_active(stamp, now=now, idle_revert_minutes=cfg.idle_revert_minutes, pinned=pinned, pin_until=pin_until)
    deadline = model_pick_deadline(conversation.model_override)
    expired = bool(deadline and now >= deadline)
    if conversation.model_override_scope == "temporary" and (
        expired or (not deadline and cfg.enabled and not active)
    ):
        conversation.model_override = None
    if conversation.reasoning_override_scope == "temporary" and cfg.enabled and not active:
        conversation.reasoning_override = None
    new_session = cfg.enabled and cfg.start_new_sessions and session_opening_pending(
        store.lookup_by_session_key(session_key), attended_new_session=conversation.attended_new_session,
    )
    if new_session and stamp is None:
        stamp = now
    decision = resolve_attended_snapshot(
        runner, source=source, user_config=user_config, override=conversation.model_override,
        model_role=conversation.model_role, model_scope=conversation.model_override_scope,
        stamp=stamp, new_session=new_session, reasoning_value=conversation.reasoning_override,
        reasoning_scope=conversation.reasoning_override_scope, attended_pinned=pinned, attended_pin_until=pin_until,
    )
    # Like turn setup, inspection cannot switch back before a quiet API boundary.
    if decision is not None and attended_switch_back_pending(
        decision, conversation.last_attended_decision, conversation.model_role,
        runner._load_attended_route_active(session_key),
    ):
        decision = conversation.last_attended_decision
    model, runtime, _notice, _cache_model = runner._load_session_agent_runtime(
        source=source, skey=session_key, user_config=user_config, config=runner.config,
        override=conversation.model_override, last_model=last_model,
        attended_route=attended_runtime_route(decision),
    )
    reasoning = conversation.reasoning_override
    if reasoning is None:
        reasoning = runner._load_attended_reasoning_config(model, user_config)
        if decision is not None and decision.reasoning_effort and decision.model == model:
            reasoning = {"enabled": decision.reasoning_effort != "none", "effort": decision.reasoning_effort}
    return model, runtime, reasoning


async def read_session_settings(runner, *, source, session_key, user_config):
    """Snapshot live state on its owning loop. Never hydrate, expire, or commit a turn."""
    while True:
        state = runner._peek_session_state(session_key)
        epoch = state.persistent.model_override_epoch if state is not None else None
        live = state.conversation if state is not None else ConversationState()
        conversation = replace(
            live,
            model_override=dict(live.model_override) if live.model_override is not None else None,
            reasoning_override=dict(live.reasoning_override) if live.reasoning_override is not None else None,
        )
        star = runner._peek_session_state("*")
        last_model = conversation.last_resolved_model or (star.conversation.last_resolved_model if star else "")
        result = await asyncio.to_thread(
            _load_session_settings, runner, source=source, session_key=session_key,
            user_config=user_config, conversation=conversation, last_model=last_model,
        )
        current = runner._peek_session_state(session_key)
        if current is state and (current is None or current.persistent.model_override_epoch == epoch):
            return result
