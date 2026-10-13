"""Every FIFO producer must honor failed durable admission, not consume its input."""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway import shutdown_flush
from gateway.platforms.event import MessageEvent
from gateway.session import build_session_key
from hermes_state import SessionDB
from tests.gateway.test_steer_command import _make_runner, _make_source, _session_entry


def _event(text, mid):
    return MessageEvent(text=text, source=_make_source(), message_id=mid)


@pytest.fixture
def failed_storage(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("injected queue checkpoint failure")
    monkeypatch.setattr(shutdown_flush, "_write_payload", fail)


def test_telegram_grace_window_does_not_consume_rejected_followup(failed_storage, monkeypatch):
    monkeypatch.setenv("HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS", "3")
    runner, adapter = _make_runner(_session_entry())
    event = _event("retain my follow-up", "grace1")
    key = build_session_key(event.source)
    runner._session_state(key).turn.started_ts = time.time()
    assert (
        runner._hm_busy_telegram_grace_queue(event, event.source, key, "queue")
        == "Queue admission could not be durably acknowledged. No queued work was started; retry when storage is available."
    )
    assert adapter._pending_messages == {}
    assert not getattr(event, "_gateway_accepted", False)


@pytest.mark.asyncio
async def test_busy_queue_rejection_is_visible_even_inside_grace_window(failed_storage, monkeypatch):
    monkeypatch.setenv("HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS", "3")
    runner, adapter = _make_runner(_session_entry())
    event = _event("retain my follow-up", "grace2")
    key = build_session_key(event.source)
    runner._session_state(key).turn.started_ts = time.time()
    runner._effective_busy_input_mode = lambda source: "queue"
    runner._draining = False
    reply = await runner._hm_handle_running_session_message(event, event.source, key)
    assert reply and "could not" in reply.lower()
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_busy_steer_fallback_rejection_is_visible(failed_storage, monkeypatch):
    monkeypatch.setenv("HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS", "0")
    runner, adapter = _make_runner(_session_entry())
    event = _event("retain failed steer fallback", "steer1")
    runner._draining = False
    runner._effective_busy_input_mode = lambda source: "steer"
    runner._queue_or_replace_pending_event = lambda *_args: False
    runner._peek_session_state = lambda _key: SimpleNamespace(
        turn=SimpleNamespace(agent=None, started_ts=0)
    )
    reply = await runner._hm_handle_running_session_message(event, event.source, build_session_key(event.source))
    assert reply and "could not" in reply.lower()
    assert adapter._pending_messages == {}


@pytest.mark.parametrize("orphan_count", [1, 3])
def test_failed_incoming_checkpoint_restores_orphans_and_runs_incoming(failed_storage, orphan_count):
    runner, adapter = _make_runner(_session_entry())
    event = _event("new arrival must not disappear", "incoming")
    key = build_session_key(event.source)
    orphans = [_event(f"orphan {i}", f"o{i}") for i in range(orphan_count)]
    runner._session_state(key).conversation.queued_events.extend(orphans)
    turn, source, internal = runner._hm_rescue_orphaned_fifo(event, event.source, False, key)
    assert turn is event
    assert source is event.source and internal is False
    assert runner._overflow_queue(key) == orphans
    assert key not in adapter._pending_messages
    assert not getattr(event, "_gateway_accepted", False)


@pytest.mark.asyncio
async def test_goal_kickoff_checkpoint_failure_reports_no_started_turn(failed_storage, monkeypatch):
    from hermes_cli import goal_command

    runner, adapter = _make_runner(_session_entry())
    event = _event("/goal inspect this", "goal1")
    runner._get_goal_manager_for_event = AsyncMock(return_value=(SimpleNamespace(session_id="sid"), None))
    runner._run_in_executor_with_context = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    result = SimpleNamespace(prompt="inspect this", kickoff=True, clear_pending=None, output="Goal set and starting.")
    monkeypatch.setattr(goal_command, "dispatch_goal_command", lambda *a, **kw: result)
    reply = await runner._handle_goal_command(event)
    assert "could not" in reply.lower() and "retry" in reply.lower()
    assert "starting" not in reply.lower()
    assert adapter._pending_messages == {}


def test_builtin_queue_checkpoint_is_discarded_once_not_warned_every_boot(tmp_path):
    event = _event("/stop", "control1")
    assert shutdown_flush.checkpoint_queued_event("k", event)
    path = event._hermes_recovery_spool
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("sid", "telegram")
        claims = []
        for _ in range(2):
            recovered, _blocked = shutdown_flush.recover_pending_spool(
                session_db=db, session_resolver=lambda *a, **kw: ("sid", db),
                deferred_followup=lambda *a: claims.append(a) or True,
            )
            assert recovered == 0
            assert not path.exists()
        assert claims == [] and db.get_messages("sid") == []
    finally:
        db.close()


def test_duplicate_queue_propagates_logical_fifo_admission():
    runner, adapter = _make_runner(_session_entry())
    original = _event("plain queue", "plain1")
    retry = _event("plain queue", "plain1")
    key = build_session_key(original.source)
    assert runner._queue_or_replace_pending_event(key, original)
    assert runner._queue_or_replace_pending_event(key, retry)
    assert adapter._pending_messages[key] is original
    assert runner._overflow_queue(key) in (None, [])
    assert getattr(retry, "_gateway_accepted", False) is True


@pytest.mark.asyncio
async def test_active_busy_handler_reports_rejected_queue_instead_of_ack(failed_storage):
    from gateway.platforms.event import MessageType

    runner, adapter = _make_runner(_session_entry())
    event = _event("photo follow-up", "busy1")
    event.message_type = MessageType.PHOTO
    runner._draining = False
    runner._is_user_authorized_for_source = lambda source: True
    runner._admit_bot_message_for_source = lambda source: True
    runner._route_plaintext_approval_while_busy = AsyncMock(return_value=False)
    runner._effective_busy_input_mode = lambda source: "queue"
    runner._resolve_busy_steer_or_redirect = AsyncMock(return_value=SimpleNamespace(
        effective_mode="queue", redirected=False, steered=False,
        demoted_for_subagents=False, demoted_for_compression=False,
    ))
    runner._queue_or_replace_pending_event = lambda *_args: False
    runner._send_busy_ack_reply = AsyncMock()
    runner._compose_busy_ack_message = lambda *a, **kw: "queued successfully"
    assert await runner._handle_active_session_busy_message(event, build_session_key(event.source))
    reply = runner._send_busy_ack_reply.await_args.args[2]
    assert "could not" in reply.lower() and "retry" in reply.lower()
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_leftover_steer_survives_fifo_rejection(failed_storage):
    runner, adapter = _make_runner(_session_entry())
    source = _make_source()
    adapter.get_pending_message.side_effect = lambda key: adapter._pending_messages.pop(key, None)
    event, pending = await runner._run_agent_drain_pending(
        {"interrupted": True, "interrupt_message": "priority", "pending_steer": "also retain this"},
        adapter, source, build_session_key(source),
    )
    assert event is None
    assert pending == "priority\n\nalso retain this"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["photo", "setup", "grace"])
@pytest.mark.parametrize("occupied", [False, True], ids=["empty", "checkpointed-head"])
@pytest.mark.parametrize("reject", [False, True], ids=["accepted", "rejected"])
async def test_pending_merge_admission_covers_running_photo_setup_and_grace(
    tmp_path, monkeypatch, route, occupied, reject,
):
    from gateway.platforms.event import MessageType
    from gateway.run import _AGENT_PENDING_SENTINEL
    from gateway.run_busy import QUEUE_ADMISSION_FAILURE

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS", "3" if route == "grace" else "0")
    runner, adapter = _make_runner(_session_entry())
    source = _make_source()
    key = build_session_key(source)
    runner._draining = False
    runner._effective_busy_input_mode = lambda source: "steer"
    state = runner._session_state(key)
    state.turn.agent = _AGENT_PENDING_SENTINEL if route == "setup" else SimpleNamespace()
    state.turn.started_ts = time.time() if route == "grace" else 0
    head = _event("head", "head") if occupied else None
    if head is not None:
        assert runner._queue_or_replace_pending_event(key, head)
        original_spool = head._hermes_recovery_spool.read_bytes()
    follow = _event("follow-up", "follow")
    if route == "photo":
        follow.message_type = MessageType.PHOTO
        follow.media_urls = [str(tmp_path / "follow.jpg")]
        follow.media_types = ["image/jpeg"]
    if reject:
        def fail(*args, **kwargs):
            raise OSError("injected merge checkpoint failure")
        monkeypatch.setattr(shutdown_flush, "_write_payload", fail)

    reply = await runner._hm_handle_running_session_message(follow, source, key)
    if reject:
        assert reply == QUEUE_ADMISSION_FAILURE
        assert adapter._pending_messages.get(key) is head
        if head is not None:
            assert head.text == "head" and head.media_urls == []
            assert head._hermes_recovery_spool.read_bytes() == original_spool
        assert not follow._gateway_accepted
        return
    assert reply is None
    merged = adapter._pending_messages[key]
    path = getattr(merged, "_hermes_recovery_spool", None)
    assert path is not None and path.exists()
    data = json.loads(path.read_text())["data"]
    assert data["text"] == merged.text and "follow-up" in data["text"]
    assert data["media_urls"] == follow.media_urls
    assert data["queue_accepted"] and data["drain_deferred"]
    assert len(list((tmp_path / "pending_messages").glob("pending-*.json"))) == 1

    # Exercise restart claiming, not only JSON serialization: the accepted merged content must be
    # offered to the next gateway exactly once with the refreshed media and text intact.
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("sid", "telegram")
        claimed = []
        shutdown_flush.recover_pending_spool(
            session_db=db, session_resolver=lambda *a, **kw: ("sid", db),
            deferred_followup=lambda k, sid, data, path: claimed.append(data) or True,
        )
        assert len(claimed) == 1
        assert claimed[0]["text"] == merged.text
        assert claimed[0]["media_urls"] == follow.media_urls
    finally:
        db.close()


def test_flush_rewrites_stale_pending_checkpoint_after_in_memory_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = _make_runner(_session_entry())
    key = build_session_key(_make_source())
    event = _event("before mutation", "stale-spool")
    assert runner._queue_or_replace_pending_event(key, event)
    path = event._hermes_recovery_spool
    event.text = "after mutation"
    assert shutdown_flush.flush_pending_to_file(adapter._pending_messages) == 1
    assert json.loads(path.read_text())["data"]["text"] == "after mutation"


def test_busy_internal_admission_failure_is_not_acknowledged():
    import asyncio

    runner, adapter = _make_runner(_session_entry())
    event = _event("internal completion", "internal-fail")
    event.internal = True
    event.allow_gateway_control = True
    key = build_session_key(event.source)
    adapter._pending_messages[key] = _event("occupied", "occupied")
    runner._queue_or_replace_pending_event = Mock(return_value=False)
    assert asyncio.run(runner._handle_active_session_busy_message(event, key)) is False


@pytest.mark.asyncio
async def test_draining_internal_admission_failure_is_not_acknowledged():
    runner, adapter = _make_runner(_session_entry())
    event = _event("internal completion", "drain-fail")
    event.internal = True
    runner._draining = True
    runner._preserve_drain_event = Mock(return_value=False)
    assert await runner._handle_active_session_busy_message(event, build_session_key(event.source)) is False


@pytest.mark.asyncio
async def test_internal_busy_admission_failure_is_not_acknowledged():
    runner, adapter = _make_runner(_session_entry())
    event = _event("internal completion", "internal-fail-normal")
    event.internal = True
    event.allow_gateway_control = False
    key = build_session_key(event.source)
    runner._draining = False
    runner._is_user_authorized_for_source = Mock(return_value=True)
    runner._admit_bot_message_for_source = Mock(return_value=True)
    runner._route_plaintext_approval_while_busy = AsyncMock(return_value=False)
    runner._queue_or_replace_pending_event = Mock(return_value=False)
    assert await runner._handle_active_session_busy_message(event, key) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("reject", [False, True], ids=["accepted", "rejected"])
async def test_failed_first_delivery_checkpoints_eventless_followup(tmp_path, monkeypatch, reject):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = _make_runner(_session_entry())
    source = _make_source()
    key = build_session_key(source)
    existing = _event("already waiting", "waiting")
    assert runner._enqueue_fifo(key, existing, adapter)
    runner._run_agent_deliver_first_response = AsyncMock(return_value=False)
    ctx = SimpleNamespace(
        source=source, session_id="sid", session_key=key, run_generation=1,
        _interrupt_depth=0, history=[], _status_thread_metadata={},
        internal=False, reply_expected=True, event_metadata={},
    )
    if reject:
        def fail(*args, **kwargs):
            raise OSError("injected deferred checkpoint failure")
        monkeypatch.setattr(shutdown_flush, "_write_payload", fail)
    result = {"messages": [], "final_response": "not delivered"}
    assert await runner._run_agent_queued_followup(
        ctx, adapter, "eventless follow-up", None, "not delivered", result, None,
    ) is result
    if reject:
        assert adapter._pending_messages[key] is existing
        assert not runner._overflow_queue(key)
    else:
        deferred = adapter._pending_messages[key]
        assert deferred.text == "eventless follow-up"
        assert json.loads(deferred._hermes_recovery_spool.read_text())["data"]["text"] == deferred.text
        assert runner._overflow_queue(key) == [existing]


@pytest.mark.parametrize("reject", [False, True], ids=["accepted", "rejected"])
def test_yuanbao_recall_replacement_requires_checkpoint(tmp_path, monkeypatch, reject):
    import asyncio
    from gateway.platforms.yuanbao import RecallGuardMiddleware

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source = _make_source()
    key = build_session_key(source)
    original = _event("already queued", "original")
    assert shutdown_flush.checkpoint_queued_event(key, original)
    original_path = original._hermes_recovery_spool
    adapter = SimpleNamespace(
        name="yuanbao", _pending_messages={key: original},
        _active_sessions={key: asyncio.Event()}, _processing_msg_texts={},
    )
    monkeypatch.setattr(RecallGuardMiddleware, "_build_source", lambda *args: source)
    if reject:
        def fail(*args, **kwargs):
            raise OSError("injected recall checkpoint failure")
        monkeypatch.setattr(shutdown_flush, "_write_payload", fail)
    RecallGuardMiddleware._interrupt_for_recall(adapter, key, "recalled", "group", "user")
    if reject:
        assert adapter._pending_messages[key] is original
        assert not adapter._active_sessions[key].is_set()
        assert original._hermes_recovery_spool.exists()
    else:
        recalled = adapter._pending_messages[key]
        assert "MESSAGE RECALLED" in recalled.text
        assert json.loads(recalled._hermes_recovery_spool.read_text())["data"]["text"] == recalled.text
        assert adapter._active_sessions[key].is_set()
        assert not original_path.exists()


@pytest.mark.asyncio
async def test_goal_continuation_rejection_reports_failed_dispatch():
    runner, adapter = _make_runner(_session_entry())
    mgr = SimpleNamespace(
        is_active=lambda: True,
        evaluate_after_turn=lambda *a, **kw: {"should_continue": True, "continuation_prompt": "continue"},
    )
    runner._post_turn_manager = AsyncMock(return_value=mgr)
    runner._session_key_for_source = build_session_key
    adapter._pending_messages = None  # disconnected adapter cannot accept a synthetic tick
    runner._enqueue_fifo = Mock(wraps=runner._enqueue_fifo)
    runner._run_in_executor_with_context = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    runner._defer_goal_status_notice_after_delivery = AsyncMock()
    await runner._post_turn_goal_continuation(
        session_entry=_session_entry(), source=_make_source(), final_response="done",
    )
    runner._enqueue_fifo.assert_called_once()
    runner._defer_goal_status_notice_after_delivery.assert_awaited_once()
    reply = runner._defer_goal_status_notice_after_delivery.await_args.args[1]
    assert "could not" in reply.lower() and "retry" in reply.lower()
    assert adapter._pending_messages is None
