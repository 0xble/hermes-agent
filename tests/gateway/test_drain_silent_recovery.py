"""Gateway drain admission remains silent and its pending turns survive shutdown."""

import asyncio
from datetime import datetime
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionEntry
from gateway.shutdown_flush import flush_overflow_to_file, flush_pending_to_file, recover_pending_to_db
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
async def test_startup_recovery_spool_requires_native_adapter_admission(tmp_path, accepted):
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    adapter.set_message_handler(AsyncMock(return_value=None))
    spool = tmp_path / "pending.json"
    spool.write_text('{"text":"preserve this follow-up"}', encoding="utf-8")
    event = MessageEvent(text="preserve this follow-up", source=source, user_id="u1",
                         metadata={} if accepted else {"gateway_session_key": "another-session"})
    event._hermes_recovered_followup = True
    event._hermes_recovery_spool = spool
    runner._startup_restore_queue = [event]
    try:
        drained = await runner._drain_startup_restore_queue()
        assert event._gateway_accepted is accepted
        assert spool.exists() is not accepted
        assert drained == int(accepted)
    finally:
        await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)


def _recover(tmp_path, monkeypatch, runner, adapter, key, expected):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    slot = dict(adapter._pending_messages)
    overflow = runner._overflow_queue(key) or []
    assert flush_pending_to_file(slot) == (1 if slot else 0)
    assert flush_overflow_to_file({key: overflow}) == len(overflow)
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


@pytest.mark.asyncio
async def test_queued_model_echo_of_interrupt_diagnostic_never_sends_before_followup():
    """A completed model turn can echo a prior interrupt diagnostic without its interrupt flag."""
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    key = runner._session_key_for_source(source)
    runner._run_agent = AsyncMock(return_value={"final_response": "follow-up answer", "messages": []})
    runner._is_goal_continuation_event = MagicMock(return_value=False)
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="follow-up")
    runner._refresh_agent_cache_message_count = AsyncMock()
    ctx = SimpleNamespace(
        session_key=key, session_id="sid", source=source, stream_consumer_holder=[None],
        mute_notification_reply=False, persist_user_display_kind=None,
        _status_thread_metadata=None, event_message_id=None, inbound_message_id="101",
        run_generation=1, _interrupt_depth=0, history=[], context_prompt=None,
        result_holder=[None], _post_delivery_adapter=None,
    )
    result = {"final_response": "Operation interrupted: retrying API call after error (retry 1/3).",
              "interrupted": False, "failed": False, "messages": []}
    followup = MessageEvent(text="follow-up", source=source, user_id="u1")
    await runner._run_agent_queued_followup(
        ctx, adapter, "follow-up", followup, result, result, None,
    )
    assert adapter.sent == []
    runner._run_agent.assert_awaited_once()


@pytest.mark.parametrize("placeholder", [
    "[No reply: this turn was interrupted before completion. Do not repeat this internal marker.]",
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
    assert _sanitize_gateway_final_response(Platform.TELEGRAM, placeholder, interrupted=False) == ""


def test_interrupted_diagnostic_mentioned_in_prose_is_retained():
    from gateway.config import Platform
    from gateway.run import _sanitize_gateway_final_response

    prose = "Operation interrupted: waiting for model response is a diagnostic, not a reply."
    assert _sanitize_gateway_final_response(Platform.TELEGRAM, prose) == prose
    marker = "[No reply: this turn was interrupted before completion. Do not repeat this internal marker.]"
    prose = f"The transcript contains {marker} and the work continues."
    assert _sanitize_gateway_final_response(Platform.TELEGRAM, prose) == prose


def test_long_complete_interrupt_diagnostic_is_suppressed_not_prose():
    from gateway.config import Platform
    from gateway.run import _sanitize_gateway_final_response

    diagnostic = f"Operation interrupted: handling API error ({'x' * 150})."
    assert _sanitize_gateway_final_response(Platform.TELEGRAM, diagnostic) == ""
    prose = f"The log said {diagnostic} and then continued."
    assert _sanitize_gateway_final_response(Platform.TELEGRAM, prose) == prose


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
@pytest.mark.parametrize("text,busy", [
    ("new turn", False), ("/custom", False), ("busy arrival", True),
])
async def test_adapter_drain_preserves_one_turn_without_reentering_handler(
    text, busy, tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    adapter.gateway_runner = runner
    source = make_restart_source()
    key = runner._session_key_for_source(source)
    runner._draining = True
    if text == "/custom":
        # Drain starts after admission, before quick/plugin dispatch: this
        # exercises the second idle drain gate through the adapter task.
        async def admit(event):
            runner._draining = False
            return event, source, False

        async def resolve(_event, _source, _key):
            runner._draining = True
            return False, None, "custom", "custom"

        runner._hm_admit_event = admit
        runner._hm_resolve_command = resolve
        runner._hm_pending_reply_intercepts = AsyncMock(return_value=None)
        runner._hm_evict_idle_stale_agent = lambda _key: None
        runner._is_session_running = lambda _key: False
        runner._hm_estop_gate = lambda *_args: None
        runner._quick_command_alias_text = lambda _event: None
        runner._hm_dispatch_canonical_command = AsyncMock(return_value=(False, None))
    else:
        runner._hm_admit_event = AsyncMock(side_effect=lambda event: (event, source, False))
    runner._session_state = lambda _key: SimpleNamespace(conversation=SimpleNamespace(queued_events=[]))
    runner._peek_session_state = runner._session_state
    calls = []

    async def handle(event):
        calls.append(event.text)
        return await runner._handle_admitted_message(event)

    adapter.set_message_handler(handle)
    busy_calls = []
    original_busy_handler = adapter._busy_session_handler

    async def busy_handler(event, session_key):
        busy_calls.append(event.text)
        return await original_busy_handler(event, session_key)

    adapter.set_busy_session_handler(busy_handler)
    event = MessageEvent(text=text, source=source, user_id="u1")
    if busy:
        # A live adapter task owns the guard; the new event takes the busy path.
        active = asyncio.Event()
        entered = asyncio.Event()
        original_handler = adapter._message_handler

        async def active_handler(first):
            entered.set()
            await active.wait()
            return None

        adapter.set_message_handler(active_handler)
        await adapter.handle_message(MessageEvent(text="existing turn", source=source, user_id="u1"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        adapter.set_message_handler(original_handler)
    await adapter.handle_message(event)
    if busy:
        assert calls == []
        active.set()
    # The active task must settle without spawning another one.
    for _ in range(300):
        if key not in adapter._session_tasks:
            break
        await asyncio.sleep(0.01)
    assert calls == ([] if busy else [text])
    assert busy_calls == ([text] if busy else [])
    assert key not in adapter._session_tasks
    assert adapter.sent == []
    assert adapter._pending_messages[key].text == text
    assert flush_pending_to_file(dict(adapter._pending_messages)) == 1

    now = datetime.now()
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
    spool = next((tmp_path / "pending_messages").glob("*.json"))
    recover = partial(
        recover_pending_to_db, db, session_resolver=lambda *_a, **_kw: ("sid", db),
        deferred_followup=partial(_defer_for_test, runner),
    )
    assert recover() == 1
    assert recover() == 0
    assert [e.text for e in runner._startup_restore_queue] == [text]
    assert spool.exists()
    assert adapter.sent == []
    db.append_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/restart", "/stop", "/status", "/approve"])
@pytest.mark.parametrize("busy", [False, True])
async def test_control_commands_dispatch_during_drain_without_replay(command, busy, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    adapter.gateway_runner = runner
    source = make_restart_source()
    key = runner._session_key_for_source(source)
    runner._draining = True
    runner._restart_requested = True
    runner._hm_admit_event = AsyncMock(side_effect=lambda event: (event, source, False))
    runner._hm_estop_gate = lambda *_args: None
    runner._hm_pending_reply_intercepts = AsyncMock(return_value=None)
    runner._hm_evict_idle_stale_agent = lambda _key: None
    runner._is_session_running = lambda _key: busy
    runner._check_slash_access = lambda *_args: None
    runner._async_profile_scope_for_source = lambda *_args: _null_async_context()
    calls = []
    for name in ("restart", "stop", "status", "approve"):
        async def handler(event, name=name):
            calls.append(name)
            return f"{name} handled"
        setattr(runner, f"_handle_{name}_command", handler)
    async def busy_dispatch(event, definition, *_args):
        return await getattr(runner, f"_handle_{definition.name}_command")(event)
    runner._dispatch_busy_slash_command = AsyncMock(side_effect=busy_dispatch)
    adapter.set_message_handler(runner._handle_admitted_message)
    if busy:
        entered, release = asyncio.Event(), asyncio.Event()
        async def active(_event):
            entered.set()
            await release.wait()
        adapter.set_message_handler(active)
        await adapter.handle_message(MessageEvent(text="active", source=source, user_id="u1"))
        await asyncio.wait_for(entered.wait(), 2)
        adapter.set_message_handler(runner._handle_admitted_message)
        # Keep the real adapter guard, but avoid changing the running turn in this fixture.
        async def dispatch_active(event, *_args):
            return await runner._handle_admitted_message(event)
        adapter._dispatch_active_session_command = AsyncMock(side_effect=dispatch_active)
    await adapter.handle_message(MessageEvent(text=command, source=source, user_id="u1"))
    if busy:
        release.set()
    for _ in range(300):
        if key not in adapter._session_tasks:
            break
        await asyncio.sleep(0.01)
    assert calls == [command[1:]]
    assert not adapter._pending_messages
    assert not list((tmp_path / "pending_messages").glob("*.json")) if (tmp_path / "pending_messages").exists() else True
    assert all("Gateway" not in sent for sent in adapter.sent)


from contextlib import asynccontextmanager

@asynccontextmanager
async def _null_async_context():
    yield


@pytest.mark.asyncio
async def test_update_during_existing_drain_does_not_schedule_another_update(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    adapter.gateway_runner = runner
    source = make_restart_source()
    runner._draining = True
    runner._hm_admit_event = AsyncMock(side_effect=lambda event: (event, source, False))
    runner._hm_estop_gate = lambda *_args: None
    runner._hm_pending_reply_intercepts = AsyncMock(return_value=None)
    runner._hm_evict_idle_stale_agent = lambda _key: None
    runner._is_session_running = lambda _key: False
    runner._check_slash_access = lambda *_args: None
    runner._async_profile_scope_for_source = lambda *_args: _null_async_context()
    runner._handle_update_command = type(runner)._handle_update_command.__get__(runner)
    runner._schedule_update_notification_watch = MagicMock()
    from gateway import update_launcher
    launch = MagicMock(side_effect=AssertionError("must not schedule update"))
    monkeypatch.setattr(update_launcher, "launch_native_update", launch)
    adapter.set_message_handler(runner._handle_admitted_message)
    await adapter.handle_message(MessageEvent(text="/update", source=source, user_id="u1"))
    key = runner._session_key_for_source(source)
    for _ in range(300):
        if key not in adapter._session_tasks:
            break
        await asyncio.sleep(0.01)
    assert not adapter._pending_messages
    assert any("progress" in sent.lower() or "already" in sent.lower() for sent in adapter.sent)
    launch.assert_not_called()
    runner._schedule_update_notification_watch.assert_not_called()


@pytest.mark.parametrize("text,age_hours,authorized", [
    ("/restart", 0, True), ("/stop", 0, True), ("/restart@HermesBot", 0, True),
    ("old turn", 25, True), ("unauthorized turn", 0, False),
])
def test_drain_recovery_drops_controls_expired_and_denied_events(
    text, age_hours, authorized, tmp_path, monkeypatch, caplog,
):
    import os
    import time
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, _adapter = make_restart_runner()
    source = make_restart_source()
    key = runner._session_key_for_source(source)
    event = MessageEvent(text=text, source=source, user_id="u1")
    event._drain_deferred = True
    assert flush_pending_to_file({key: event}) == 1
    spool = next((tmp_path / "pending_messages").glob("*.json"))
    if age_hours:
        then = time.time() - age_hours * 3600
        os.utime(spool, (then, then))
    now = datetime.now()
    runner.session_store._lock = MagicMock()
    runner.session_store._ensure_loaded_locked = lambda: None
    runner.session_store._entries[key] = SessionEntry(
        session_key=key, session_id="sid", created_at=now, updated_at=now, origin=source)
    runner._is_session_running = lambda _key: False
    runner._restored_source = lambda entry: entry.origin
    runner._resume_owner_authorized = lambda *_args: authorized
    runner._startup_restore_queue = []
    db = MagicMock()
    from gateway.run_pending_recovery import _defer_followup
    assert recover_pending_to_db(
        db, session_resolver=lambda *_a, **_kw: ("sid", db),
        deferred_followup=partial(_defer_followup, runner, {}, None),
    ) == 0
    assert not spool.exists()
    assert not runner._startup_restore_queue
    db.append_message.assert_not_called()
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_full_drain_fifo_replays_original_arrival_order(tmp_path, monkeypatch):
    from datetime import timedelta
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    key = runner._session_key_for_source(source)
    runner._BUSY_QUEUE_MAX_PENDING = 1
    earlier = MessageEvent(text="earlier", source=source, user_id="u1",
                           timestamp=datetime.now() - timedelta(seconds=15))
    later = MessageEvent(text="later", source=source, user_id="u1", timestamp=datetime.now())
    runner._preserve_drain_event(key, earlier)
    runner._preserve_drain_event(key, later)
    assert flush_pending_to_file(dict(adapter._pending_messages)) == 1
    runner.session_store._lock = MagicMock()
    runner.session_store._ensure_loaded_locked = lambda: None
    runner.session_store._entries[key] = SessionEntry(
        session_key=key, session_id="sid", created_at=datetime.now(), updated_at=datetime.now(), origin=source)
    runner._is_session_running = lambda _key: False
    runner._restored_source = lambda entry: entry.origin
    runner._resume_owner_authorized = lambda *_args: True
    runner._startup_restore_queue = []
    db = MagicMock()
    from gateway.run_pending_recovery import _defer_followup
    assert recover_pending_to_db(db, session_resolver=lambda *_a, **_kw: ("sid", db),
                                 deferred_followup=partial(_defer_followup, runner, {}, None)) == 2
    assert [event.text for event in runner._startup_restore_queue] == ["earlier", "later"]


def _defer_for_test(runner, key, sid, data, path):
    from gateway.run_pending_recovery import _defer_followup
    return _defer_followup(runner, {}, None, key, sid, data, path)


def test_non_drain_followup_platform_mismatch_can_fall_back_to_history(tmp_path):
    from gateway.config import Platform
    from gateway.run_pending_recovery import _defer_followup

    runner, _adapter = make_restart_runner()
    key = runner._session_key_for_source(make_restart_source())
    source = make_restart_source()
    source_for_entry = source.__class__(platform=Platform.SLACK, chat_id=source.chat_id,
                                         chat_type=source.chat_type, user_id=source.user_id)
    entry = SessionEntry(session_key=key, session_id="sid", created_at=datetime.now(),
                         updated_at=datetime.now(), origin=source_for_entry, resume_pending=True)
    runner.session_store._lock = MagicMock()
    runner.session_store._ensure_loaded_locked = lambda: None
    runner.session_store._entries[key] = entry
    runner._is_session_running = lambda _key: False
    runner._restored_source = lambda _entry: source
    runner._resume_owner_authorized = lambda *_args: True
    runner._auto_resume_ready = lambda *_args, **_kw: (_adapter, source)
    runner._startup_restore_queue = []
    assert _defer_followup(runner, {key: entry}, Platform.SLACK, key, "sid",
                           {"text": "ordinary followup", "source_user_id": "u1"},
                           tmp_path / "ordinary.json") is True
    assert [event.text for event in runner._startup_restore_queue] == ["ordinary followup"]


@pytest.mark.asyncio
async def test_non_drain_busy_queue_still_queues():
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    key = runner._session_key_for_source(source)
    runner._busy_input_mode = "queue"
    runner._is_user_authorized_for_source = lambda _s: True
    runner._admit_bot_message_for_source = lambda _s: True
    event = MessageEvent(text="normal queue", source=source, user_id="u1")
    assert await runner._handle_active_session_busy_message(event, key) is True
    assert adapter._pending_messages[key].text == "normal queue"


@pytest.mark.asyncio
@pytest.mark.parametrize("cap,connected", [(0, True), (1, False)])
async def test_idle_adapter_drain_spools_when_fifo_cannot_admit(tmp_path, monkeypatch, cap, connected):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    adapter.gateway_runner = runner
    source = make_restart_source()
    key = runner._session_key_for_source(source)
    runner._draining = True
    runner._BUSY_QUEUE_MAX_PENDING = cap
    runner._hm_admit_event = AsyncMock(side_effect=lambda event: (event, source, False))
    runner._session_state = lambda _key: SimpleNamespace(conversation=SimpleNamespace(queued_events=[]))
    runner._peek_session_state = runner._session_state
    if not connected:
        runner.adapters.clear()
    adapter.set_message_handler(runner._handle_admitted_message)
    event = MessageEvent(text="preserve me", source=source, user_id="u1")
    await adapter.handle_message(event)
    for _ in range(300):
        if key not in adapter._session_tasks:
            break
        await asyncio.sleep(0.01)
    assert event._gateway_accepted is True  # stamped by the idle adapter before dispatch
    assert key not in adapter._pending_messages
    assert len(list((tmp_path / "pending_messages").glob("*.json"))) == 1


def test_ordinary_queued_head_precedes_later_drain_spool(tmp_path, monkeypatch):
    from datetime import timedelta
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    key = runner._session_key_for_source(source)
    head = MessageEvent(text="head", source=source, user_id="u1",
                        timestamp=datetime.now() - timedelta(seconds=30))
    drain = MessageEvent(text="drain", source=source, user_id="u1",
                         timestamp=datetime.now() - timedelta(seconds=5))
    assert flush_pending_to_file({key: head}) == 1
    import json
    head_spool, = (tmp_path / "pending_messages").glob("*.json")
    assert json.loads(head_spool.read_text())["ts"] == head.timestamp.timestamp()
    runner._BUSY_QUEUE_MAX_PENDING = 0
    runner._preserve_drain_event(key, drain)
    now = datetime.now()
    runner.session_store._lock = MagicMock()
    runner.session_store._ensure_loaded_locked = lambda: None
    entry = runner.session_store._entries[key] = SessionEntry(
        session_key=key, session_id="sid", created_at=now, updated_at=now,
        origin=source, resume_pending=True)
    runner._is_session_running = lambda _key: False
    runner._restored_source = lambda entry: entry.origin
    runner._resume_owner_authorized = lambda *_args: True
    runner._auto_resume_ready = lambda *_args, **_kw: (adapter, source)
    runner._startup_restore_queue = []
    from gateway.run_pending_recovery import _defer_followup
    db = MagicMock()
    assert recover_pending_to_db(db, session_resolver=lambda *_a, **_kw: ("sid", db),
                                 deferred_followup=partial(_defer_followup, runner, {key: entry}, None)) == 2
    assert [event.text for event in runner._startup_restore_queue] == ["head", "drain"]


def test_failed_drain_authorization_keeps_spool_for_next_pass(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    key = runner._session_key_for_source(source)
    event = MessageEvent(text="keep", source=source, user_id="u1")
    event._drain_deferred = True
    assert flush_pending_to_file({key: event}) == 1
    now = datetime.now()
    runner.session_store._lock = MagicMock()
    runner.session_store._ensure_loaded_locked = lambda: None
    runner.session_store._entries[key] = SessionEntry(
        session_key=key, session_id="sid", created_at=now, updated_at=now, origin=source)
    runner._is_session_running = lambda _key: False
    runner._restored_source = lambda entry: entry.origin
    runner._is_user_authorized_for_source = MagicMock(side_effect=OSError("allowlist unavailable"))
    runner._startup_restore_queue = []
    from gateway.run_pending_recovery import _defer_followup
    db = MagicMock()
    recover = partial(recover_pending_to_db, db, session_resolver=lambda *_a, **_kw: ("sid", db),
                      deferred_followup=partial(_defer_followup, runner, {}, None))
    assert recover() == 0
    assert len(list((tmp_path / "pending_messages").glob("*.json"))) == 1
    runner._is_user_authorized_for_source = MagicMock(return_value=True)
    assert recover() == 1
    assert [e.text for e in runner._startup_restore_queue] == ["keep"]


@pytest.mark.asyncio
async def test_reconnect_during_drain_does_not_claim_arrival(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    key = runner._session_key_for_source(source)
    event = MessageEvent(text="later", source=source, user_id="u1")
    event._drain_deferred = True
    assert flush_pending_to_file({key: event}, reason="drain_arrival") == 1
    now = datetime.now()
    runner.session_store._lock = MagicMock()
    runner.session_store._ensure_loaded_locked = lambda: None
    runner.session_store._entries[key] = SessionEntry(
        session_key=key, session_id="sid", created_at=now, updated_at=now, origin=source)
    runner._is_session_running = lambda _key: False
    runner._restored_source = lambda entry: entry.origin
    runner._resume_owner_authorized = lambda *_args: True
    runner._startup_restore_queue = []
    from gateway.run_pending_recovery import _defer_followup
    db = MagicMock()
    runner._draining = True
    assert recover_pending_to_db(db, session_resolver=lambda *_a, **_kw: ("sid", db),
                                 deferred_followup=partial(_defer_followup, runner, {}, source.platform,
                                                           reconnect_recovery=True)) == 0
    assert runner._startup_restore_queue == []
    assert len(list((tmp_path / "pending_messages").glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_unbound_adapter_does_not_dispatch_deferred_head():
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    key = runner._session_key_for_source(source)
    adapter.gateway_runner = None
    deferred = MessageEvent(text="later", source=source, user_id="u1")
    deferred._drain_deferred = True
    adapter._pending_messages[key] = deferred
    adapter._session_tasks[key] = asyncio.current_task()
    spawn = MagicMock()
    adapter._spawn_drain_task = spawn
    adapter._finish_session_task(key, asyncio.Event())
    spawn.assert_not_called()
    assert adapter._pending_messages[key] is deferred
