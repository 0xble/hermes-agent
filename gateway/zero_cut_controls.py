"""Route handover controls to the original in-memory owner, never replay a turn."""
from __future__ import annotations

import asyncio

from gateway.config import is_zero_cut_handover_enabled


def callback_key(data: str):
    parts = data.split(":")
    if len(parts) != 3:
        return None
    if parts[0] == "ea":
        try:
            return "_approval_state", int(parts[2])
        except ValueError:
            return None
    if parts[0] == "sc":
        return "_slash_confirm_state", parts[2]
    if parts[0] == "cl":
        return "_clarify_state", parts[1]
    return None


def owns_callback(adapter, data: str) -> bool:
    key = callback_key(data)
    return bool(key and key[1] in getattr(adapter, key[0], {}))


async def forward_callback(adapter, update) -> bool:
    runner = getattr(adapter, "gateway_runner", None)
    if not is_zero_cut_handover_enabled(getattr(runner, "config", None)):
        return False
    data = update.callback_query.data
    if callback_key(data) is None or owns_callback(adapter, data):
        return False
    from gateway.gateway_slot import effective_gateway_slot
    from gateway.status import _get_process_hermes_home
    from gateway.control_socket import query_gateway_control
    other = "b" if effective_gateway_slot() == "a" else "a"
    home = _get_process_hermes_home()
    state = await asyncio.to_thread(query_gateway_control, home, "handover-status", slot=other)
    if not (state or {}).get("token_lock_released"):
        return False
    # A submitted resolution is never retried on a lost answer. Its external outcome is ambiguous.
    answer = await asyncio.to_thread(query_gateway_control, home, "handover-callback", slot=other,
                                   params={"bot_id": adapter._app.bot.id, "update": update.to_dict()})
    return answer is None or answer.get("owned", False)


async def resolve_callback(runner, params: dict) -> dict:
    from gateway.zero_cut_handover_drain import _telegram_adapters
    from telegram import Update
    if not (getattr(runner, "_zero_cut_handover_state", None) or {}).get("token_lock_released"):
        return {"owned": False}
    for adapter in _telegram_adapters(runner):
        if adapter._app.bot.id != params.get("bot_id"):
            continue
        update = Update.de_json(params["update"], adapter._app.bot)
        query = update.callback_query
        if query is None or not owns_callback(adapter, query.data or ""):
            return {"owned": False}
        # Re-enter the same transport/routed profile scope used by native callbacks.
        cb = adapter._callback_ctx(query)
        async with adapter._session_control_callback_scope(query, cb, str(query.from_user.id)):
            await adapter._handle_callback_query(update, None)
        return {"owned": True}
    return {"owned": False}


async def resolve_text_control(runner, params: dict) -> dict:
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource
    from gateway.zero_cut_busy import read_active_turn_marker
    import os
    source = SessionSource.from_dict(params["source"])
    event = MessageEvent(text=params["text"], source=source)
    key = runner._session_key_for_source(source)
    marker = await asyncio.to_thread(read_active_turn_marker, runner.async_session_store, key)
    if not marker or marker["pid"] != os.getpid():
        return {"owned": False}
    verb = event.text.split()[0].split("@")[0].lower()
    handlers = {"/approve": runner._handle_approve_command, "/deny": runner._handle_deny_command,
                "/stop": runner._handle_stop_command}
    if verb not in handlers:
        return {"owned": False}
    async with runner._async_profile_scope_for_source(source):
        response = await handlers[verb](event)
        return {"owned": True, "response": response if isinstance(response, str) else None}
