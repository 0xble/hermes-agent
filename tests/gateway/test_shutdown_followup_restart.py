"""A shutdown follow-up is its own future turn, not the predecessor's resume marker."""

import asyncio
from functools import partial
from unittest.mock import Mock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run_pending_recovery import _defer_followup
from gateway.session import SessionStore
from gateway.shutdown_flush import flush_pending_to_file, recover_pending_to_db
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "managed"))
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    entry = store.get_or_create_session(source)
    runner.session_store = store
    runner._startup_restore_queue = []
    runner._is_user_authorized_for_source = lambda source: True
    runner._restored_source = lambda entry: entry.origin
    runner._intake_adapter_for = lambda source: adapter
    runner._delivery_adapter_for = lambda source: adapter
    runner._thread_metadata_for_source = lambda source: {}
    return runner, adapter, entry, source


@pytest.mark.asyncio
@pytest.mark.parametrize("marker_state", ["missing", "cleared", "pending"])
async def test_shutdown_followup_starts_own_turn_without_predecessor_resume_marker(tmp_path, monkeypatch, marker_state, caplog):
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    if marker_state != "missing":
        runner.session_store.mark_resume_pending(entry.session_key, turn_id="answered-predecessor")
    if marker_state == "cleared":
        runner.session_store.clear_resume_pending(entry.session_key)
    event = MessageEvent(text="the queued question", source=source, user_id=source.user_id)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    db = Mock()
    assert recover_pending_to_db(
        db, session_resolver=lambda *args, **kwargs: (entry.session_id, db),
        deferred_followup=partial(_defer_followup, runner, {}, None),
    ) == 1
    db.append_message.assert_not_called()
    assert [event.text for event in runner._startup_restore_queue] == ["the queued question"]
    called = []

    async def handle(event):
        called.append(event.text)
        event._agent_turn_succeeded = True

    adapter.set_message_handler(handle)
    with caplog.at_level("INFO", logger="gateway.run"):
        assert runner._schedule_resume_pending_sessions(candidates=[entry]) == 0
        assert await runner._drain_startup_restore_queue() == 1
        await asyncio.gather(*list(adapter._background_tasks))
        await asyncio.gather(*list(runner._background_tasks))
    assert called == ["the queued question"]
    assert not list((tmp_path / "pending_messages").glob("*.json"))
    outcomes = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Restart recovery outcome:")]
    assert outcomes == [f"Restart recovery outcome: session={entry.session_key} outcome=resumed"]


@pytest.mark.asyncio
async def test_restored_followup_accepted_without_turn_gets_note_and_skipped_outcome(tmp_path, monkeypatch, caplog):
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="saved question", source=source, user_id=source.user_id)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    db = Mock()
    recover_pending_to_db(db, session_resolver=lambda *args, **kwargs: (entry.session_id, db),
                          deferred_followup=partial(_defer_followup, runner, {}, None))

    async def accept_but_drop(event):
        event._gateway_accepted = True

    adapter.handle_message = accept_but_drop
    with caplog.at_level("INFO", logger="gateway.run"):
        await runner._drain_startup_restore_queue()
        await asyncio.gather(*list(runner._background_tasks))
    assert len(adapter.sent) == 1
    assert "follow-up" in adapter.sent[0]
    outcomes = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Restart recovery outcome:")]
    assert outcomes == [f"Restart recovery outcome: session={entry.session_key} outcome=skipped:accepted-without-turn:noted"]
    assert entry.session_key not in runner._restart_followup_keys
    assert list((tmp_path / "pending_messages").glob("*.json"))


@pytest.mark.asyncio
async def test_started_recovered_followup_retires_spool_before_later_restart(tmp_path, monkeypatch):
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="do the side effect", source=source, user_id=source.user_id)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    db = Mock()
    recover_pending_to_db(
        db,
        session_resolver=lambda *args, **kwargs: (entry.session_id, db),
        deferred_followup=partial(_defer_followup, runner, {}, None),
    )
    spool, = (tmp_path / "pending_messages").glob("*.json")
    started = asyncio.Event()
    release = asyncio.Event()

    async def turn():
        started.set()
        await release.wait()

    async def handle(recovered):
        recovered._gateway_accepted = True
        recovered._agent_turn_succeeded = False
        adapter._session_tasks[entry.session_key] = asyncio.create_task(turn())

    adapter.handle_message = handle
    assert await runner._drain_startup_restore_queue() == 1
    await asyncio.wait_for(started.wait(), 2)
    assert not spool.exists()

    observers = list(runner._background_tasks)
    for task in observers:
        task.cancel()
    await asyncio.gather(*observers, return_exceptions=True)
    owner = adapter._session_tasks.pop(entry.session_key)
    owner.cancel()
    await asyncio.gather(owner, return_exceptions=True)

    fresh_runner, _adapter, fresh_entry, _source = _setup(tmp_path, monkeypatch)
    assert recover_pending_to_db(
        Mock(),
        session_resolver=lambda *args, **kwargs: (fresh_entry.session_id, Mock()),
        deferred_followup=partial(_defer_followup, fresh_runner, {}, None),
    ) == 0
    assert fresh_entry.session_key not in runner._restart_followup_keys


def test_reconnect_followup_with_missing_origin_is_retained_without_attribute_error(tmp_path, monkeypatch):
    runner, _adapter, entry, _source = _setup(tmp_path, monkeypatch)
    entry.origin = None
    path = tmp_path / "pending.json"
    data = {"drain_deferred": True, "text": "saved", "source_user_id": "u1"}
    assert _defer_followup(
        runner, {entry.session_key: entry}, Platform.TELEGRAM, entry.session_key,
        entry.session_id, data, path, reconnect_recovery=True,
    ) is None
