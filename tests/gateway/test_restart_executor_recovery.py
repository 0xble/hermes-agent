"""A refused executor admission is not delivery of a durable restart input."""

import asyncio
import json
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway import restart_inbox as inbox
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run_executor import GatewayCapacityError, GatewayExecutor
from gateway.session import SessionSource, SessionStore
from gateway.turn_context import TurnContext
from hermes_state import SessionDB
from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.fixture
def linked_turn(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0")
    monkeypatch.setattr(inbox, "_db_path", lambda: home / "state.db")
    store = SessionStore(sessions_dir=home / "sessions", config=GatewayConfig())
    store._routing_home = home
    store._db = db = SessionDB(db_path=home / "state.db")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="fixture-chat", user_id="fixture-owner", chat_type="dm")
    entry = store.get_or_create_session(source)
    original = MessageEvent(text="inspect exactly this input", source=source, message_type=MessageType.TEXT,
                            media_urls=["/tmp/fixture.png"], media_types=["image/png"],
                            turn_reasoning_config={"effort": "high"})
    inbox.record_event(entry.session_key, original)
    orphan(home / "state.db")
    event = inbox.claim_recoverable(deliverable_targets={("telegram", "default")})[0]["event"]
    runner, adapter = make_restart_runner()
    runner.session_store = store
    runner._session_db = db
    runner._executor_lock = threading.Lock()
    runner._executor_closing = False
    runner._executor = GatewayExecutor(max_workers=1)
    try:
        yield runner, adapter, entry, event, home / "state.db"
    finally:
        runner._executor.shutdown(wait=True)
        db.close()


def orphan(path):
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE restart_inbox SET owner_pid=999999999, owner_started_at=1")


def wire_fake_model(runner, entry, event, monkeypatch, model):
    """Keep real handler, _run_agent, executor and durable cleanup; replace UI/model I/O."""
    ctx = TurnContext(source=event.source, session_key=entry.session_key, session_id=entry.session_id)

    def build(_disp, _agent, **kwargs):
        ctx.persist_user_display_metadata = kwargs["persist_user_display_metadata"]
        return ctx, SimpleNamespace(run_sync=model), None

    monkeypatch.setattr(runner, "_get_proxy_url", lambda: None)
    monkeypatch.setattr(runner, "_run_agent_display_settings", lambda source: SimpleNamespace(
        needs_progress_queue=False, log_mode_enabled=False, _native_slack_task_cards=False))
    monkeypatch.setattr(runner, "_run_agent_build_turn_context", build)
    for name in ("_run_agent_bind_turn_wiring", "_run_agent_start_streaming_tts",
                 "_run_agent_evict_on_fallback", "_run_agent_schedule_bubble_cleanup",
                 "_schedule_goal_after_delivery"):
        monkeypatch.setattr(runner, name, MagicMock())
    for name in ("_run_agent_stream_consumer_task", "_run_agent_track_agent",
                 "_run_agent_monitor_for_interrupt", "_run_agent_notify_long_running",
                 "_run_agent_finalize_streaming_tts", "_run_agent_mark_streamed_delivery"):
        monkeypatch.setattr(runner, name, AsyncMock())
    monkeypatch.setattr(runner, "_run_agent_drain_pending", AsyncMock(return_value=(None, None)))

    async def cleanup(_ctx, **tasks):
        for task in tasks.values():
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in tasks.values() if task is not None), return_exceptions=True)

    monkeypatch.setattr(runner, "_run_agent_cleanup_turn_tasks", cleanup)
    monkeypatch.setattr(runner, "_hmwa_resolve_session", AsyncMock(return_value=(event.source, entry, entry.session_key)))
    prepared = runner._PreparedTurn([], "", event.text, event.text, None, None,
                                    entry.session_id, event._restart_inbox_claim["input_owner"])
    monkeypatch.setattr(runner, "_hmwa_prepare_turn", AsyncMock(return_value=(prepared, {})))
    monkeypatch.setattr(runner, "_clear_session_env", lambda tokens: None)
    monkeypatch.setattr(runner, "_hmwa_stop_typing_for_turn", AsyncMock())
    monkeypatch.setattr(runner, "_is_session_run_current", lambda *args: True)
    # Normal post-model persistence must not manufacture ingestion after refusal.
    monkeypatch.setattr(runner, "_hmwa_shape_agent_response", AsyncMock(
        return_value=("refused", False, [])))
    monkeypatch.setattr(runner, "_hmwa_prepend_reasoning", lambda result, response, *args: response)
    monkeypatch.setattr(runner, "_hmwa_runtime_footer_line", lambda *args: "")
    monkeypatch.setattr(runner, "_hmwa_post_turn_hooks", AsyncMock())
    monkeypatch.setattr(runner, "_hmwa_classify_turn_failure", lambda *args: (False, False, False))
    monkeypatch.setattr(runner, "_hmwa_compression_exhaustion_reset", AsyncMock(return_value=("refused", entry)))
    monkeypatch.setattr(runner, "_hmwa_deliver_turn_response", AsyncMock(return_value="refused"))
    return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["replay", "ingested", "read_failure", "primary_failure", "stale_token"])
async def test_capacity_refusal_keeps_exact_link_until_proven_recovery(linked_turn, monkeypatch, recovery):
    runner, _adapter, entry, event, path = linked_turn
    store = runner.session_store
    owner = event._restart_inbox_claim["input_owner"]
    assert await runner._mark_durable_active_turn(event, entry.session_key)
    token = entry.active_turn_token
    if recovery == "ingested":
        store._db.append_message(entry.session_id, "user", event.text,
                                 display_metadata={"gateway_input_owner": owner})
    before_messages = store._db.get_messages(entry.session_id)
    ran = []
    wire_fake_model(runner, entry, event, monkeypatch, lambda: ran.append(True))
    release, started = threading.Event(), threading.Event()

    def block():
        started.set()
        assert release.wait(10)

    first = runner._executor.submit(block)
    second = None
    try:
        assert started.wait(5)
        second = runner._executor.submit(lambda: None)
        response = await runner._handle_message_with_agent(event, event.source, entry.session_key, 1)
        assert ran == []
        assert store.has_input_owner(entry.session_id, owner) is (recovery == "ingested")
        # A newer owner is not rewritten by the rejected event's late unwind.
        if recovery == "stale_token":
            token = store.mark_turn_active(entry.session_key, restart_claim=event._restart_inbox_claim)
        assert not await runner._clear_durable_active_turn(event)
        assert "not started" in response
        assert store._db.get_messages(entry.session_id) == before_messages
        assert entry.active_turn_token == token
        assert entry.restart_inbox_link["input_owner"] == owner
        assert inbox.read_rows(path)[0]["state"] == "attempting"
        persisted = json.loads(store._db.load_gateway_routing_entries(scope=store._routing_scope())[entry.session_key])
        assert persisted["active_turn_token"] == token
        assert persisted["restart_inbox_link"] == entry.restart_inbox_link
        runner._run_agent_drain_pending.assert_not_awaited()
    finally:
        release.set()
        first.result(timeout=5)
        if second is not None:
            second.result(timeout=5)

    orphan(path)
    if recovery == "read_failure":
        monkeypatch.setattr(store, "has_input_owner", MagicMock(side_effect=OSError("unreadable proof")))
    if recovery == "primary_failure":
        before = entry.to_dict()
        with monkeypatch.context() as failing:
            failing.setattr(store._db, "save_gateway_routing_entry", MagicMock(side_effect=OSError("primary failed")))
            with pytest.raises(OSError, match="primary failed"):
                store.reconcile_restart_inbox([path])
        assert entry.to_dict() == before
    blocked = store.reconcile_restart_inbox([path])[str(path)]
    replays = inbox.claim_recoverable(deliverable_targets={("telegram", "default")}, excluded_queue_ids=blocked)
    if recovery in {"ingested", "read_failure"}:
        assert replays == []
        assert entry.restart_inbox_link["mode"] == ("continuation" if recovery == "ingested" else "parked")
        assert entry.resume_pending is (recovery == "ingested")
    else:
        assert len(replays) == 1
        replay = replays[0]["event"]
        assert replay._restart_inbox_claim["input_owner"] == owner
        assert inbox.serialize_event(replay) == inbox.serialize_event(event)
        assert not entry.resume_pending
        assert inbox.claim_recoverable(deliverable_targets={("telegram", "default")}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_error", [False, True])
async def test_started_worker_cannot_claim_admission_refusal(linked_turn, worker_error):
    runner, _adapter, entry, event, path = linked_turn
    assert await runner._mark_durable_active_turn(event, entry.session_key)
    owner = event._restart_inbox_claim["input_owner"]
    ctx = TurnContext(source=event.source, session_key=entry.session_key, session_id=entry.session_id,
                      persist_user_display_metadata={"gateway_input_owner": owner})
    ran = []

    def model():
        ran.append(True)
        runner.session_store._db.append_message(entry.session_id, "user", event.text,
                                               display_metadata={"gateway_input_owner": owner})
        if worker_error:
            raise GatewayCapacityError()
        return {"final_response": "done"}

    worker = runner._run_agent_start_turn_worker(ctx, model)
    result = await asyncio.wait_for(worker.executor_task, 5)
    assert ran == [True]
    assert not result.get("execution_not_started")
    assert runner.session_store.has_input_owner(entry.session_id, owner)
    assert await runner._clear_durable_active_turn(event)
    assert entry.restart_inbox_link is None
    assert inbox.read_rows(path)[0]["state"] == "delivered"
    orphan(path)
    assert inbox.claim_recoverable(deliverable_targets={("telegram", "default")}) == []
