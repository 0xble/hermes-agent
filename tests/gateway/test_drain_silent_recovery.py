"""Gateway drain admission remains silent and its pending turns survive shutdown."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionEntry
from gateway.shutdown_flush import flush_overflow_to_file, flush_pending_to_file, recover_pending_to_db
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _recover(tmp_path, monkeypatch, runner, adapter, key, expected):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    slot = dict(adapter._pending_messages)
    overflow = runner._overflow_queue(key) or []
    assert flush_pending_to_file(slot) == (1 if slot else 0)
    assert flush_overflow_to_file({key: overflow}) == len(overflow)
    from functools import partial
    from gateway.run_pending_recovery import _defer_followup
    now = datetime.now()
    source = make_restart_source()
    runner.session_store._lock = MagicMock()
    runner.session_store._ensure_loaded_locked = lambda: None
    runner.session_store._entries[key] = SessionEntry(
        session_key=key, session_id="sid", created_at=now, updated_at=now, origin=source,
    )
    runner._is_session_running = lambda _key: False
    runner._restored_source = lambda entry: entry.origin
    runner._resume_owner_authorized = lambda *_args: True
    runner._startup_restore_queue = []
    db = MagicMock()
    assert recover_pending_to_db(
        db, session_resolver=lambda *_a, **_kw: ("sid", db),
        deferred_followup=partial(_defer_followup, runner, {}, None),
    ) == len(expected)
    assert [event.text for event in runner._startup_restore_queue] == expected
    assert runner._startup_restore_queue[0].internal is True
    assert runner._startup_restore_queue[0].metadata["notification_category"] == "watch"
    assert runner._startup_restore_queue[1].internal is False
    db.append_message.assert_not_called()

    # A session still running during startup cannot consume the future turn yet.
    # Its on-disk copy must remain a pending turn, not become transcript history.
    runner._startup_restore_queue.clear()
    runner._is_session_running = lambda _key: True
    assert recover_pending_to_db(
        db, session_resolver=lambda *_a, **_kw: ("sid", db),
        deferred_followup=partial(_defer_followup, runner, {}, None),
    ) == 0
    db.append_message.assert_not_called()
    assert len(list((tmp_path / "pending_messages").glob("*.json"))) == len(expected)


@pytest.mark.asyncio
async def test_busy_drain_silently_recovers_internal_and_human_events(tmp_path, monkeypatch):
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    key = runner._session_key_for_source(source)
    runner._draining = True
    runner._running_agents[key] = object()
    runner._is_user_authorized_for_source = lambda _s: True
    runner._admit_bot_message_for_source = lambda _s: True
    for text, internal in (("internal completion", True), ("human follow-up", False)):
        event = MessageEvent(
            text=text, source=source, user_id="u1", internal=internal,
            metadata={"notification_category": "watch"} if internal else {},
        )
        assert await runner._handle_active_session_busy_message(event, key) is True
    assert adapter.sent == []
    _recover(tmp_path, monkeypatch, runner, adapter, key, ["internal completion", "human follow-up"])


@pytest.mark.asyncio
async def test_idle_drain_silently_recovers_human_message(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    key = runner._session_key_for_source(source)
    runner._draining = True
    runner._is_user_authorized_for_source = lambda _s: True
    runner._admit_bot_message_for_source = lambda _s: True
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._hm_pre_gateway_dispatch_hook = AsyncMock(side_effect=lambda event, _source: event)
    event = MessageEvent(text="new turn", source=source, user_id="u1")
    assert await runner._handle_admitted_message(event) is None
    assert adapter.sent == []
    runner._is_session_running = lambda _key: False
    runner.session_store._lock = MagicMock()
    runner.session_store._ensure_loaded_locked = lambda: None
    now = datetime.now()
    runner.session_store._entries[key] = SessionEntry(
        session_key=key, session_id="sid", created_at=now, updated_at=now, origin=source,
    )
    runner._restored_source = lambda entry: entry.origin
    runner._resume_owner_authorized = lambda *_args: True
    runner._auto_resume_ready = lambda entry, **_kwargs: (adapter, entry.origin)
    runner._startup_restore_queue = []
    assert flush_pending_to_file(dict(adapter._pending_messages)) == 1
    from functools import partial
    from gateway.run_pending_recovery import _defer_followup
    db = MagicMock()
    assert recover_pending_to_db(
        db, session_resolver=lambda *_a, **_kw: ("sid", db),
        deferred_followup=partial(_defer_followup, runner, {}, None),
    ) == 1
    assert [item.text for item in runner._startup_restore_queue] == ["new turn"]
    db.append_message.assert_not_called()


@pytest.mark.asyncio
async def test_queued_interrupt_placeholder_does_not_send_before_followup():
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    ctx = SimpleNamespace(
        session_key="agent:main:telegram:dm:123456", stream_consumer_holder=[None],
        mute_notification_reply=False, persist_user_display_kind=None, source=source,
        _status_thread_metadata=None, event_message_id=None, inbound_message_id="101",
    )
    result = {"final_response": "Operation interrupted: waiting for model response (1.3s elapsed).",
              "interrupted": True, "failed": False}
    assert await runner._run_agent_deliver_first_response(ctx, adapter, result, result, None)
    assert adapter.sent == []
    runner._run_agent = AsyncMock(return_value={"final_response": "follow-up answer", "messages": []})
    runner._is_goal_continuation_event = MagicMock(return_value=False)
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="follow-up")
    runner._refresh_agent_cache_message_count = AsyncMock()
    ctx.session_id = "sid"
    ctx.run_generation = 1
    ctx._interrupt_depth = 0
    ctx.history = []
    ctx.context_prompt = None
    ctx.result_holder = [None]
    followup = MessageEvent(text="follow-up", source=source, user_id="u1", internal=True)
    await runner._run_agent_queued_followup(ctx, adapter, "follow-up", followup, result, result, None)
    runner._run_agent.assert_awaited_once()


@pytest.mark.parametrize("placeholder", [
    "Operation interrupted.",
    "Operation interrupted: handling API error (timeout).",
    "Operation interrupted during retry (timeout, attempt 1/3).",
    "Operation interrupted: waiting for the provider to recover (cycle 1/2).",
    "Operation interrupted: retrying empty response from model (retry 1/3).",
])
def test_interrupted_turn_placeholders_are_not_chat_replies(placeholder):
    from gateway.config import Platform
    from gateway.run import _sanitize_gateway_final_response

    assert _sanitize_gateway_final_response(Platform.TELEGRAM, placeholder, interrupted=True) == ""
    if "waiting for model response" not in placeholder:
        assert _sanitize_gateway_final_response(Platform.TELEGRAM, placeholder, interrupted=False) == placeholder


@pytest.mark.asyncio
async def test_running_session_drain_branch_queues_without_notice():
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    key = runner._session_key_for_source(source)
    runner._draining = True
    runner._hm_busy_slash_or_photo = AsyncMock(return_value=(False, None))
    runner._hm_busy_telegram_grace_queue = lambda *_args: False
    runner._effective_busy_input_mode = lambda _source: "interrupt"
    runner._peek_session_state = lambda _key: SimpleNamespace(
        turn=SimpleNamespace(agent=object()), conversation=SimpleNamespace(queued_events=[]),
    )
    event = MessageEvent(text="while running", source=source, user_id="u1")
    assert await runner._hm_handle_running_session_message(event, source, key) is None
    assert adapter.sent == []
    assert adapter._pending_messages[key].text == event.text


@pytest.mark.asyncio
async def test_idle_quick_command_drain_gate_queues_without_notice():
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    runner._draining = True
    event = MessageEvent(text="/custom", source=source, user_id="u1")
    assert await runner._hm_dispatch_quick_and_plugin_commands(event, source, "custom") == (True, None, "custom")
    assert adapter.sent == []
    assert adapter._pending_messages[runner._session_key_for_source(source)] is event


@pytest.mark.asyncio
async def test_non_drain_busy_queue_still_queues():
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    key = runner._session_key_for_source(source)
    runner._busy_input_mode = "queue"
    runner._is_user_authorized_for_source = lambda _s: True
    runner._admit_bot_message_for_source = lambda _s: True
    event = MessageEvent(text="normal queue", source=source, user_id="u1")
    assert await runner._handle_active_session_busy_message(event, key) in (True, False)
    assert adapter._pending_messages[key].text == "normal queue"
