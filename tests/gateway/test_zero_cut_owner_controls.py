"""Owner-directed approvals use the real local control server, never a replayed turn.

Both gateways share a disposable routing DB. Telegram Bot API calls are captured,
not sent, and native supervisors are not involved.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from gateway import run, zero_cut_busy, zero_cut_standby
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from hermes_state import SessionDB
from tests.waiting import async_wait_until
from tools.approval import resolve_gateway_approval, unregister_gateway_notify
from tools.approval_gateway_wait import _await_gateway_decision


@pytest.fixture
def telegram_sdk():
    # The shared gateway conftest installs a Telegram stub before collection.
    # Decode the forwarded Update with the real optional SDK, then restore that
    # exact module state so this test does not change other files' dependencies.
    from tests.gateway._plugin_adapter_loader import load_plugin_adapter

    def selected(name):
        return name == "telegram" or name.startswith("telegram.") or name == "plugin_adapter_telegram"

    saved = {name: module for name, module in sys.modules.copy().items() if selected(name)}
    for name in saved:
        sys.modules.pop(name)
    try:
        sdk = pytest.importorskip("telegram", reason="real Telegram SDK required for serialized callback round-trip")
        yield sdk, load_plugin_adapter("telegram")
    finally:
        for name in list(sys.modules):
            if selected(name):
                sys.modules.pop(name)
        sys.modules.update(saved)


@pytest_asyncio.fixture
async def waiting_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    owner = run.GatewayRunner(GatewayConfig(zero_cut_handover_enabled=True, sessions_dir=tmp_path / "sessions"))
    owner.session_store._db = SessionDB(tmp_path / "state.db")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="456", chat_type="dm")
    entry = owner.session_store.get_or_create_session(source)
    token = owner.session_store.mark_turn_active(entry.session_key)
    owner._zero_cut_handover_state = {"accepted": True, "updates_settled": True, "token_lock_released": True}
    owner.on_message = AsyncMock(side_effect=AssertionError("approval must not replay a model turn"))
    owner._deliver_approval_confirmation = AsyncMock(return_value="resolved")
    notified = threading.Event()
    waiter = asyncio.create_task(asyncio.to_thread(
        _await_gateway_decision, entry.session_key, lambda data: notified.set(),
        {"command": "test command", "session_key": entry.session_key},
    ))
    server = await run._start_gateway_start_control_socket(owner)
    try:
        assert server is not None
        assert await asyncio.to_thread(notified.wait, 5)
        yield owner, source, entry.session_key, waiter
        owner.on_message.assert_not_called()
    finally:
        resolve_gateway_approval(entry.session_key, "deny")
        unregister_gateway_notify(entry.session_key)
        await asyncio.wait_for(waiter, 5)
        owner.session_store.clear_turn_active(entry.session_key, token)
        if server is not None:
            await server.stop()
        owner.session_store._db.close()
        zero_cut_standby.reset_for_tests()


@pytest.mark.asyncio
@pytest.mark.parametrize("command,choice", [("/approve", "once"), ("/deny", "deny")])
async def test_text_resolution_bypasses_already_queued_followup(waiting_owner, monkeypatch, tmp_path, command, choice):
    _owner, source, key, waiter = waiting_owner
    successor = run.GatewayRunner(GatewayConfig(zero_cut_handover_enabled=True, sessions_dir=tmp_path / "sessions"))
    successor.session_store._db = SessionDB(tmp_path / "state.db")
    # Only the successor's process identity differs. The receiver still validates its actual PID.
    monkeypatch.setattr(zero_cut_busy, "os", SimpleNamespace(getpid=lambda: os.getpid() + 1))
    transport = SimpleNamespace(_pending_messages={}, send=AsyncMock())
    successor.adapters[Platform.TELEGRAM] = transport
    monkeypatch.setattr(successor, "_delivery_adapter_for", lambda src: transport)
    followup = MessageEvent("queued follow-up", source=source, message_id="follow-up")
    monkeypatch.setattr(successor, "_queue_or_replace_pending_event",
                        lambda k, event: transport._pending_messages.setdefault(k, event) is event)
    try:
        assert await zero_cut_busy.queue_foreign_owner(successor, followup, key)
        assert key in successor._zero_cut_busy_keys
        assert await zero_cut_busy.queue_foreign_owner(successor, MessageEvent(command, source=source), key)
        # A queued /approve cannot release the original waiter. Observe the actual resolution first.
        await async_wait_until(waiter.done, timeout=2)
        assert waiter.result()["choice"] == choice
        assert transport._pending_messages[key] is followup
        transport.send.assert_awaited_once_with("123", "resolved", thread_id=None)
    finally:
        successor.session_store._db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_forwarded_inline_button_preserves_original_approval_and_authorization(waiting_owner, telegram_sdk, monkeypatch, authorized):
    sdk, adapter_module = telegram_sdk
    TelegramAdapter = adapter_module.TelegramAdapter
    Update = sdk.Update

    owner, _source, key, waiter = waiting_owner
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "456")
    bot = AsyncMock()
    bot.id = 789
    bot.defaults = None
    bot.send_message.return_value = SimpleNamespace(message_id=99)
    old = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    new = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    for adapter in (old, new):
        adapter._bot = bot
        adapter._app = SimpleNamespace(bot=bot)
        adapter.gateway_runner = owner
    owner.adapters[Platform.TELEGRAM] = old
    assert (await old.send_exec_approval("123", "test command", key)).success
    data = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    assert not new._approval_state
    monkeypatch.setattr("gateway.gateway_slot.effective_gateway_slot", lambda: "b")
    update = Update.de_json({
        "update_id": 100,
        "callback_query": {"id": "tap", "chat_instance": "chat", "data": data,
                           "from": {"id": 456 if authorized else 999, "is_bot": False, "first_name": "Caller"},
                           "message": {"message_id": 99, "date": 1, "text": "Approve command?",
                                       "chat": {"id": 123, "type": "private"}}},
    }, bot)
    from gateway import control_socket
    calls = []
    query_control = control_socket.query_gateway_control

    def capture_control(*args, **kwargs):
        answer = query_control(*args, **kwargs)
        calls.append((args[1], answer))
        return answer

    monkeypatch.setattr(control_socket, "query_gateway_control", capture_control)
    await new._handle_callback_query(update, None)
    if authorized:
        assert not old._approval_state, (calls, bot.answer_callback_query.await_args, bot.edit_message_text.await_args)
        await async_wait_until(waiter.done, timeout=2)
        assert waiter.result()["choice"] == "once"
        assert not old._approval_state
        bot.edit_message_text.assert_awaited_once()
    else:
        assert not waiter.done()
        assert old._approval_state
        bot.edit_message_text.assert_not_called()
    assert not new._approval_state
    bot.answer_callback_query.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_slash_confirm_routes_to_original_session_while_successor_has_its_own_prompt(
    waiting_owner, telegram_sdk, monkeypatch, tmp_path, authorized,
):
    from tools import slash_confirm

    sdk, adapter_module = telegram_sdk
    owner, source, old_key, _waiter = waiting_owner
    successor = run.GatewayRunner(GatewayConfig(zero_cut_handover_enabled=True, sessions_dir=tmp_path / "new-sessions"))
    new_source = SessionSource(platform=Platform.TELEGRAM, chat_id="987", user_id="456", chat_type="dm")
    new_key = successor._session_key_for_source(new_source)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "456")
    bot = AsyncMock()
    bot.id = 789
    bot.defaults = None
    bot.send_message.return_value = SimpleNamespace(message_id=99)
    old_handler, new_handler = AsyncMock(return_value="old resolved"), AsyncMock(return_value="new resolved")
    adapters = []
    for runner in (owner, successor):
        adapter = adapter_module.TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
        adapter._bot = bot
        adapter._app = SimpleNamespace(bot=bot)
        adapter.gateway_runner = runner
        runner.adapters[Platform.TELEGRAM] = adapter
        adapters.append(adapter)
    old, new = adapters
    try:
        for runner, target, handler in ((owner, source, old_handler), (successor, new_source, new_handler)):
            await runner._request_slash_confirm(event=MessageEvent("/reset", source=target), command="reset",
                                                title="Reset", message="Reset this session?", handler=handler)
        old_id, new_id = next(iter(old._slash_confirm_state)), next(iter(new._slash_confirm_state))
        assert old_id != new_id
        data = f"sc:once:{old_id}"
        assert len(data.encode("utf-8")) <= 64
        monkeypatch.setattr("gateway.gateway_slot.effective_gateway_slot", lambda: "b")
        update = sdk.Update.de_json({
            "update_id": 101,
            "callback_query": {"id": "old-slash-tap", "chat_instance": "chat", "data": data,
                "from": {"id": 456 if authorized else 999, "is_bot": False, "first_name": "Caller"},
                "message": {"message_id": 99, "date": 1, "text": "Reset this session?",
                            "chat": {"id": 123, "type": "private"}}},
        }, bot)
        await new._handle_callback_query(update, None)
        new_handler.assert_not_awaited()
        assert slash_confirm.get_pending(new_key)["confirm_id"] == new_id
        if authorized:
            old_handler.assert_awaited_once_with("once")
            assert slash_confirm.get_pending(old_key) is None
            # A repeated tap cannot execute either handler again.
            await new._handle_callback_query(update, None)
            old_handler.assert_awaited_once()
            new_handler.assert_not_awaited()
        else:
            old_handler.assert_not_awaited()
            assert slash_confirm.get_pending(old_key)["confirm_id"] == old_id
    finally:
        slash_confirm.clear(old_key)
        slash_confirm.clear(new_key)
        successor.session_store.close_all_db_handles()
        successor.close_all_session_db_handles()
