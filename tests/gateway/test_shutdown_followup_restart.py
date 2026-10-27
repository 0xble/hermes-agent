"""A shutdown follow-up is its own future turn, not the predecessor's resume marker."""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone, UTC
from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import _reply_anchor_for_event, merge_pending_message_event
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run_pending_recovery import _defer_followup, _recovered_followup_event
from gateway.session import SessionStore
from gateway.shutdown_flush import flush_pending_to_file, recover_pending_to_db
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source
from tests.waiting import async_wait_until


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


def test_recovered_internal_event_keeps_recovery_key_out_of_transport_identity(tmp_path, monkeypatch):
    _runner, _adapter, _entry, source = _setup(tmp_path, monkeypatch)
    recovered = _recovered_followup_event(
        {
            "text": "internal completion",
            "internal": True,
            "recovery_id": "hermes-recovery-test",
            "user_id": source.user_id,
        },
        replace(source, platform=Platform.SLACK),
        source.user_id,
    )
    assert recovered.message_id is None
    assert recovered.source.message_id is None
    assert recovered._hermes_recovery_id == "hermes-recovery-test"
    assert _reply_anchor_for_event(recovered) is None


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
async def test_restored_followup_failed_real_turn_gets_no_preserved_notice(tmp_path, monkeypatch, caplog):
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="saved question", source=source, user_id=source.user_id)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    db = Mock()
    recover_pending_to_db(db, session_resolver=lambda *args, **kwargs: (entry.session_id, db),
                          deferred_followup=partial(_defer_followup, runner, {}, None))

    async def turn(recovered):
        recovered._agent_turn_succeeded = False

    async def handle(recovered):
        recovered._gateway_accepted = True
        adapter._session_tasks[entry.session_key] = asyncio.create_task(turn(recovered))

    adapter.handle_message = handle
    with caplog.at_level("INFO", logger="gateway.run"):
        await runner._drain_startup_restore_queue()
        await asyncio.gather(*list(runner._background_tasks))
    # The spool is retired, so a "still preserved" notice would be false; the turn reported itself.
    assert adapter.sent == []
    assert not list((tmp_path / "pending_messages").glob("*.json"))
    outcomes = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Restart recovery outcome:")]
    assert outcomes == [f"Restart recovery outcome: session={entry.session_key} outcome=skipped:turn-failed"]
    assert entry.session_key not in runner._restart_followup_keys




@pytest.mark.asyncio
async def test_durable_turn_failed_followup_logs_after_predecessor_resumed(
        tmp_path, monkeypatch, caplog):
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="saved question", source=source, user_id=source.user_id)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    db = Mock()
    recover_pending_to_db(db, session_resolver=lambda *args, **kwargs: (entry.session_id, db),
                          deferred_followup=partial(_defer_followup, runner, {}, None))
    recovered = runner._startup_restore_queue[0]
    runner.session_store.append_to_transcript(entry.session_id, {
        "role": "user", "content": recovered.text,
        "platform_message_id": recovered._hermes_recovery_id,
    })

    async def turn(recovered):
        recovered._agent_turn_succeeded = False

    async def handle(recovered):
        recovered._gateway_accepted = True
        adapter._session_tasks[entry.session_key] = asyncio.create_task(turn(recovered))

    adapter.handle_message = handle
    with caplog.at_level("INFO", logger="gateway.run"):
        runner._record_restart_resume_outcome(entry.session_key, "resumed")
        await runner._drain_startup_restore_queue()
        await asyncio.gather(*list(runner._background_tasks))
    outcomes = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Restart recovery outcome:")]
    assert outcomes == [
        f"Restart recovery outcome: session={entry.session_key} outcome=resumed",
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:turn-failed",
    ]


@pytest.mark.asyncio
async def test_recovered_followup_resumed_logs_after_predecessor_resumed(
        tmp_path, monkeypatch, caplog):
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="fresh follow-up", source=source, user_id=source.user_id)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    recover_pending_to_db(
        Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
        deferred_followup=partial(_defer_followup, runner, {}, None),
    )

    async def handle(recovered):
        recovered._gateway_accepted = True
        recovered._agent_turn_succeeded = True

    adapter.handle_message = handle
    with caplog.at_level("INFO", logger="gateway.run"):
        runner._record_restart_resume_outcome(entry.session_key, "resumed")
        assert await runner._drain_startup_restore_queue() == 1
        await asyncio.gather(*list(runner._background_tasks))
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=resumed",
        f"Restart recovery outcome: session={entry.session_key} outcome=resumed",
    ]


@pytest.mark.asyncio
async def test_recovered_followup_with_both_ids_probes_transport_id_before_replay(
        tmp_path, monkeypatch, caplog):
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(
        text="already handled", source=source, user_id=source.user_id, message_id="transport-1")
    event._hermes_recovery_id = "recovery-1"
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    runner.session_store.append_to_transcript(entry.session_id, {
        "role": "user", "content": event.text, "platform_message_id": event.message_id,
    })
    claimed = []

    async def handle(recovered):
        claimed.append(recovered.text)
        recovered._agent_turn_succeeded = True

    adapter.handle_message = handle
    with caplog.at_level("INFO", logger="gateway.run"):
        assert recover_pending_to_db(
            Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
            deferred_followup=partial(_defer_followup, runner, {}, None)) == 1
        assert await runner._drain_startup_restore_queue() == 0
        await asyncio.gather(*list(runner._background_tasks))
    assert claimed == []
    assert len(adapter.sent) == 1 and "may already have been handled" in adapter.sent[0]
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:possibly-duplicate:noted"]


@pytest.mark.asyncio
async def test_legacy_recovered_followup_exception_uses_content_durability_probe(
        tmp_path, monkeypatch, caplog):
    runner, adapter, entry, _source = _setup(tmp_path, monkeypatch)
    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir(parents=True, exist_ok=True)
    spool = flush_dir / "pending-legacy-exception.json"
    spool_ts = time.time() - 10
    spool.write_text(json.dumps({
        "session_key": entry.session_key, "reason": "shutdown_follow_up", "ts": spool_ts, "seq": -1,
        "data": {"text": "legacy follow-up", "source_user_id": entry.origin.user_id,
                 "user_id": entry.origin.user_id},
    }))
    assert recover_pending_to_db(
        Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
        deferred_followup=partial(_defer_followup, runner, {}, None)) == 1
    recovered = runner._startup_restore_queue.pop()
    runner.session_store.append_to_transcript(entry.session_id, {
        "role": "user", "content": recovered.text, "timestamp": time.time(),
    })

    async def fail(_event):
        raise RuntimeError("turn failed after durable input")

    recovered._gateway_accepted = True
    task = asyncio.create_task(fail(recovered))
    task._hermes_event = recovered
    with caplog.at_level("INFO", logger="gateway.run"):
        await runner._observe_recovered_followup(adapter, recovered, entry.session_key, task=task)
    assert len(adapter.sent) == 1
    assert "still preserved" not in adapter.sent[0]
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:turn-failed:noted"]
    assert not spool.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["adapter-offline", "dispatch-failed"])
async def test_recovered_followup_failure_logs_after_predecessor_outcome(
        tmp_path, monkeypatch, caplog, failure):
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="follow-up", source=source, user_id=source.user_id)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    assert recover_pending_to_db(
        Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
        deferred_followup=partial(_defer_followup, runner, {}, None)) == 1
    if failure == "adapter-offline":
        runner._intake_adapter_for = lambda _source: None
        runner._delivery_adapter_for = lambda _source: None
    else:
        async def reject(_event):
            raise RuntimeError("dispatch failed")
        adapter.handle_message = reject
    with caplog.at_level("INFO", logger="gateway.run"):
        runner._record_restart_resume_outcome(entry.session_key, "resumed")
        assert await runner._drain_startup_restore_queue() == 0
        await asyncio.gather(*list(runner._background_tasks))
    suffix = "note-unavailable" if failure == "adapter-offline" else "noted"
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=resumed",
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:{failure}:{suffix}",
    ]


@pytest.mark.asyncio
async def test_internal_recovered_followup_consumed_by_successor_reports_result(tmp_path, monkeypatch, caplog):
    """A pending-slot successor owns the turn result, not the parked recovery event."""
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(
        text="internal completion", source=source, user_id=source.user_id,
        internal=True, message_type=MessageType.PHOTO,
    )
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    recover_pending_to_db(Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
                          deferred_followup=partial(_defer_followup, runner, {}, None))
    recovered = runner._startup_restore_queue.pop()
    recovered._gateway_accepted = True
    consumed = MessageEvent(text="internal completion", source=source, user_id=source.user_id, internal=True,
                             message_type=MessageType.PHOTO)
    adapter._pending_messages[entry.session_key] = consumed
    merge_pending_message_event(adapter._pending_messages, entry.session_key, recovered)

    async def successor():
        runner.session_store.append_to_transcript(entry.session_id, {"role": "user", "content": consumed.text})
        consumed._agent_turn_succeeded = True

    task = asyncio.create_task(successor())
    task._hermes_event = consumed
    adapter._session_tasks[entry.session_key] = task
    with caplog.at_level("INFO", logger="gateway.run"):
        await runner._observe_recovered_followup(adapter, recovered, entry.session_key, task=task)
    assert not list((tmp_path / "pending_messages").glob("*.json"))
    outcomes = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Restart recovery outcome:")]
    assert outcomes == [f"Restart recovery outcome: session={entry.session_key} outcome=resumed"]


@pytest.mark.asyncio
async def test_merged_internal_recovery_spool_retires_after_successor_input_is_durable(
    tmp_path, monkeypatch
):
    """A merged successor's durable input retires the recovered event's spool before completion."""
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(
        text="internal completion", source=source, user_id=source.user_id,
        internal=True, message_type=MessageType.PHOTO,
    )
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    recover_pending_to_db(
        Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
        deferred_followup=partial(_defer_followup, runner, {}, None),
    )
    recovered = runner._startup_restore_queue.pop()
    spool = recovered._hermes_recovery_spool
    consumed = MessageEvent(
        text="existing photo", source=source, user_id=source.user_id,
        internal=True, message_type=MessageType.PHOTO,
    )
    adapter._pending_messages[entry.session_key] = consumed
    merge_pending_message_event(adapter._pending_messages, entry.session_key, recovered)
    assert getattr(recovered, "_hermes_absorbed_into", None) is consumed

    persisted = asyncio.Event()
    release = asyncio.Event()

    async def successor():
        runner.session_store.append_to_transcript(
            entry.session_id, {
                "role": "user", "content": consumed.text,
                "platform_message_id": recovered._hermes_recovery_id,
            }
        )
        persisted.set()
        await release.wait()
        consumed._agent_turn_succeeded = True

    task = asyncio.create_task(successor())
    task._hermes_event = consumed
    adapter._session_tasks[entry.session_key] = task
    observer = asyncio.create_task(
        runner._observe_recovered_followup(adapter, recovered, entry.session_key, task=task)
    )
    await asyncio.wait_for(persisted.wait(), 2)
    for _ in range(40):
        if not spool.exists():
            break
        await asyncio.sleep(0.05)
    assert not spool.exists(), "merged successor durability must retire the replay copy mid-turn"
    release.set()
    await asyncio.wait_for(observer, 2)


@pytest.mark.asyncio
async def test_recovery_probe_accepts_merged_successor_transport_id(tmp_path, monkeypatch):
    """A merged successor's real platform id is durable for its recovered predecessor."""
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    recovered = MessageEvent(
        text="internal completion", source=source, user_id=source.user_id,
        internal=True, message_type=MessageType.PHOTO,
    )
    assert flush_pending_to_file({entry.session_key: recovered}, reason="shutdown_follow_up") == 1
    recover_pending_to_db(
        Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
        deferred_followup=partial(_defer_followup, runner, {}, None),
    )
    recovered = runner._startup_restore_queue.pop()
    successor = MessageEvent(
        text="existing photo", source=source, user_id=source.user_id,
        message_id="transport-successor", internal=True, message_type=MessageType.PHOTO,
    )
    adapter._pending_messages[entry.session_key] = successor
    merge_pending_message_event(adapter._pending_messages, entry.session_key, recovered)
    runner.session_store.append_to_transcript(entry.session_id, {
        "role": "user", "content": successor.text, "platform_message_id": successor.message_id,
    })

    assert await runner._recovered_input_durable(recovered)


@pytest.mark.asyncio
async def test_recovery_ignores_unrelated_inflight_result_before_merged_successor(
    tmp_path, monkeypatch, caplog
):
    """The real busy merge follows the successor, not an unrelated predecessor result."""
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    recovered_source_event = MessageEvent(
        text="internal completion", message_type=MessageType.PHOTO,
        source=source, user_id=source.user_id, internal=True,
    )
    assert flush_pending_to_file(
        {entry.session_key: recovered_source_event}, reason="shutdown_follow_up"
    ) == 1
    recover_pending_to_db(
        Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
        deferred_followup=partial(_defer_followup, runner, {}, None),
    )
    recovered = runner._startup_restore_queue.pop()
    predecessor = MessageEvent(
        text="predecessor", message_type=MessageType.PHOTO,
        source=source, user_id=source.user_id, internal=True,
    )
    successor_event = MessageEvent(
        text="successor", message_type=MessageType.PHOTO,
        source=source, user_id=source.user_id, internal=True,
    )
    predecessor_done, successor_done = asyncio.Event(), asyncio.Event()

    async def handle(event):
        if event is predecessor:
            await predecessor_done.wait()
            event._agent_turn_succeeded = False
        elif event is successor_event:
            await successor_done.wait()
            event._agent_turn_succeeded = True

    adapter.set_message_handler(handle)
    adapter._start_typing_refresh = lambda *args, **kwargs: None
    adapter._stop_typing_refresh = AsyncMock()
    adapter._run_processing_hook = AsyncMock()
    adapter._release_turn_marker = AsyncMock()
    adapter._fire_post_delivery_callback = AsyncMock()
    adapter._flush_text_debounce_now = AsyncMock()

    adapter._pending_messages[entry.session_key] = successor_event
    merge_pending_message_event(adapter._pending_messages, entry.session_key, recovered)
    assert recovered in getattr(successor_event, "_hermes_absorbed_events", [])
    predecessor_task = asyncio.create_task(
        adapter._process_message_background(predecessor, entry.session_key)
    )
    successor_task = asyncio.create_task(
        adapter._process_message_background(successor_event, entry.session_key)
    )
    adapter._session_tasks[entry.session_key] = predecessor_task
    with caplog.at_level("INFO", logger="gateway.run"):
        observer = asyncio.create_task(
            runner._observe_recovered_followup(
                adapter, recovered, entry.session_key, task=predecessor_task
            )
        )
        predecessor_done.set()
        await asyncio.sleep(0)
        adapter._session_tasks[entry.session_key] = successor_task
        successor_done.set()
        await asyncio.gather(observer, predecessor_task, successor_task)

    assert getattr(recovered, "_agent_turn_succeeded", None) is True
    outcomes = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Restart recovery outcome:")]
    assert outcomes == [f"Restart recovery outcome: session={entry.session_key} outcome=resumed"]


@pytest.mark.asyncio
async def test_idless_recovered_followup_whose_input_is_already_durable_without_resume_marker_gets_hedged_note(
        tmp_path, monkeypatch, caplog):
    """A durable follow-up without an admitted resume marker is declined, not replayed."""
    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="internal completion", source=source, user_id=source.user_id, internal=True)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    spool = next((tmp_path / "pending_messages").glob("*.json"))
    recovery_id = json.loads(spool.read_text())["data"]["recovery_id"]
    assert recovery_id.startswith("hermes-recovery-")
    runner.session_store.append_to_transcript(entry.session_id, {
        "role": "user", "content": "internal completion", "platform_message_id": recovery_id,
    })
    with caplog.at_level("INFO", logger="gateway.run"):
        assert recover_pending_to_db(
            Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
            deferred_followup=partial(_defer_followup, runner, {}, None)) == 1
        assert await runner._drain_startup_restore_queue() == 0
        await asyncio.gather(*list(runner._background_tasks))
    assert runner._startup_restore_queue == []
    assert len(_adapter.sent) == 1
    assert "may already have been handled" in _adapter.sent[0]
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:possibly-duplicate:noted"]
    assert not list((tmp_path / "pending_messages").glob("*.json"))
    assert list((tmp_path / "pending_messages").glob("*.bad"))


@pytest.mark.asyncio
async def test_recovered_followup_real_gateway_turn_persists_recovery_id_at_turn_start(tmp_path, monkeypatch):
    """The admitted gateway path forwards and persists an internal recovery key."""
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="internal side effect", source=source, user_id=source.user_id, internal=True)
    recovery_id = "hermes-recovery-real-path"
    event._hermes_recovery_id = recovery_id

    class _RecordingAsyncStore:
        def __init__(self):
            self._store = self
            self.rows = []

        def incognito_enabled(self, _session_key):
            return False

        async def append_to_transcript(self, _session_id, row, **_kwargs):
            self.rows.append(dict(row))

        async def update_session(self, *_args, **_kwargs):
            return None

        async def clear_resume_pending(self, *_args, **_kwargs):
            return None

    async_store = _RecordingAsyncStore()
    runner.session_store = async_store
    runner._async_session_store = async_store
    runner._session_db = None
    runner._hm_admit_event = AsyncMock(return_value=(event, source, True))
    runner._hm_is_registered_command = lambda _event: False
    runner._hm_estop_gate = lambda *_args: None
    runner._hm_pending_reply_intercepts = AsyncMock(return_value=None)
    runner._hm_evict_idle_stale_agent = lambda *_args: None
    runner._hm_evict_reaped_agent = AsyncMock()
    runner._is_session_running = lambda _key: False
    runner._hm_dispatch_idle_commands = AsyncMock(return_value=(False, None))
    runner._external_drain_active = False
    runner._claim_active_session_slot = lambda *_args: (None, None)
    runner._hm_rescue_orphaned_fifo = lambda event, source, internal, _key: (event, source, internal)
    state = SimpleNamespace(
        turn=SimpleNamespace(lease=None, agent=None, event=None, started_ts=None),
        conversation=SimpleNamespace(last_turn_started_at=None),
    )
    runner._session_state = lambda _key: state
    runner._begin_session_run_generation = lambda _key: 1
    runner._persist_active_agents = lambda: None
    runner._hmwa_resolve_session = AsyncMock(return_value=(source, entry, entry.session_key))
    runner._hmwa_prepare_turn = AsyncMock(return_value=(
        runner._PreparedTurn([], "", event.text, None, None, None, entry.session_id), {}
    ))
    runner._set_session_env = lambda _context: None
    runner._clear_session_env = lambda _tokens: None
    runner._is_session_run_current = lambda *_args: True
    runner._run_post_turn_hooks = AsyncMock()
    runner._hmwa_stop_typing_for_turn = AsyncMock()
    runner._hmwa_shape_agent_response = AsyncMock(return_value=("", False, []))
    runner._hmwa_prepend_reasoning = lambda _result, response, *_args: response
    runner._hmwa_post_turn_hooks = AsyncMock()
    runner._hmwa_classify_turn_failure = lambda *_args: (False, False, False)
    runner._hmwa_compression_exhaustion_reset = AsyncMock(side_effect=lambda _result, response, entry, *_args: (response, entry))
    runner._hmwa_deliver_turn_response = AsyncMock(return_value=None)
    runner._clear_durable_active_turn = AsyncMock()
    runner._release_running_agent_state = Mock()
    runner._release_turn_lease = Mock()
    runner._delivery_adapter_for = lambda _source: adapter
    runner._restore_pending_one_turn_model_override = Mock()
    runner._quick_command_alias_text = lambda _event: None
    runner._run_agent = AsyncMock(return_value={
        "final_response": "",
        "messages": [],
        "tools": [],
        "history_offset": 0,
    })

    await runner._handle_message(event)

    assert runner._run_agent.await_args.kwargs["recovery_id"] == recovery_id
    user_rows = [row for row in async_store.rows if row.get("role") == "user"]
    assert user_rows and user_rows[-1]["platform_message_id"] == recovery_id
    assert "message_id" not in user_rows[-1]


@pytest.mark.asyncio
async def test_idless_recovered_followup_spool_retires_when_input_row_lands(tmp_path, monkeypatch):
    """The durability watcher must retire a no-id spool at turn start, before the result."""
    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="internal side effect", source=source, user_id=source.user_id, internal=True)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    recover_pending_to_db(Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
                          deferred_followup=partial(_defer_followup, runner, {}, None))
    spool = next((tmp_path / "pending_messages").glob("*.json"))
    payload = json.loads(spool.read_text())
    recovery_id = payload["data"]["recovery_id"]
    assert recovery_id.startswith("hermes-recovery-")
    recovered = runner._startup_restore_queue[0]
    assert recovered.message_id is None
    assert recovered.source.message_id is None
    assert recovered._hermes_recovery_id == recovery_id
    runner.session_store.append_to_transcript(entry.session_id, {
        "role": "user", "content": event.text, "platform_message_id": recovery_id,
    })
    await asyncio.wait_for(runner._retire_spool_once_input_durable(recovered), 2)
    assert not spool.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("note_sent", [True, False])
async def test_restored_followup_exception_after_input_retirement_gets_turn_failed_note(
        tmp_path, monkeypatch, caplog, note_sent):
    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="saved question", source=source, user_id=source.user_id)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    spool, = (tmp_path / "pending_messages").glob("*.json")
    payload = json.loads(spool.read_text())
    recovery_id = payload["data"]["recovery_id"]
    recovered = _recovered_followup_event(payload["data"], source, source.user_id)
    recovered._hermes_recovered_followup = True
    recovered._hermes_recovery_session_key = entry.session_key
    recovered._hermes_recovery_session_id = entry.session_id
    recovered._hermes_recovery_spool_ts = payload["ts"]
    recovered._hermes_recovery_spool = spool
    recovered._gateway_accepted = True
    runner.session_store.append_to_transcript(entry.session_id, {
        "role": "user", "content": recovered.text, "platform_message_id": recovery_id,
    })
    runner._follow_startup_resume_turn = AsyncMock(side_effect=RuntimeError("turn exploded"))
    if not note_sent:
        async def refuse(_source, _text):
            return False
        runner._deliver_platform_notice = refuse
    with caplog.at_level("INFO", logger="gateway.run"):
        await runner._observe_recovered_followup(_adapter, recovered, entry.session_key)
    from agent.turn_failure_copy import PARTIAL_FAILED_TURN_NOTICE
    # The spool is gone, so the turn-failed note is sent and the "still preserved" one is not.
    assert _adapter.sent == ([PARTIAL_FAILED_TURN_NOTICE] if note_sent else [])
    assert not spool.exists()
    # The input is durable (no spool to keep), so the outcome reports the note's real fate.
    suffix = ":noted" if note_sent else ":note-unavailable"
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:turn-failed{suffix}"]


@pytest.mark.asyncio
async def test_restored_followup_turn_failed_note_cancel_still_logs_once(tmp_path, monkeypatch, caplog):
    """Shutdown cancelling the failed-turn note still logs one outcome and releases the key."""
    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="saved question", source=source, user_id=source.user_id)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    spool, = (tmp_path / "pending_messages").glob("*.json")
    payload = json.loads(spool.read_text())
    recovered = _recovered_followup_event(payload["data"], source, source.user_id)
    recovered._hermes_recovered_followup = True
    recovered._hermes_recovery_session_key = entry.session_key
    recovered._hermes_recovery_session_id = entry.session_id
    recovered._hermes_recovery_spool_ts = payload["ts"]
    recovered._hermes_recovery_spool = spool
    recovered._gateway_accepted = True
    runner.session_store.append_to_transcript(entry.session_id, {
        "role": "user", "content": recovered.text,
        "platform_message_id": payload["data"]["recovery_id"],
    })
    runner._restart_followup_keys = {entry.session_key}
    runner._follow_startup_resume_turn = AsyncMock(side_effect=RuntimeError("turn exploded"))

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    runner._deliver_recovered_followup_note = cancelled
    with caplog.at_level("INFO", logger="gateway.run"), pytest.raises(asyncio.CancelledError):
        await runner._observe_recovered_followup(_adapter, recovered, entry.session_key)
    assert entry.session_key not in runner._restart_followup_keys
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:turn-failed:note-unavailable"]


@pytest.mark.asyncio
async def test_restored_followup_notice_is_sent_inside_its_profile_scope(tmp_path, monkeypatch):
    """A multiplexed secondary profile's follow-up notice resolves that profile's runtime."""
    from gateway import run

    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    profile_home = tmp_path / "profiles" / "secondary"
    runner._resolve_profile_home_for_source = lambda _source: profile_home
    active = []
    entered = []

    class _SpyScope:
        def __init__(self, home):
            self.home = home

        async def __aenter__(self):
            entered.append(self.home)
            active.append(self.home)
            return self

        async def __aexit__(self, *exc):
            active.pop()
            return False

    monkeypatch.setattr(run, "_async_profile_runtime_scope", _SpyScope)
    seen = []
    from gateway import run_restart_recovery

    def translate(key, *args, **kwargs):
        # The locale is a property of the active profile scope: resolve it as t() would.
        return f"{key}@{active[-1] if active else 'launch'}"

    monkeypatch.setattr(run_restart_recovery, "t", translate)
    sent = []

    async def deliver_text(_source, text):
        seen.append(list(active))
        sent.append(text)
        return True

    runner._deliver_platform_notice = deliver_text
    event = MessageEvent(text="saved question", source=source, user_id=source.user_id)
    await runner._finish_recovered_followup(event, entry.session_key, "accepted-without-turn")
    assert entered == [profile_home]
    assert seen == [[profile_home]]
    # The text itself must be translated inside the scope, not before entering it.
    assert sent == [f"gateway.restart.followup_recovery_notice@{profile_home}"]


@pytest.mark.asyncio
async def test_recovered_followup_spool_retires_at_durable_turn_start_not_admission(tmp_path, monkeypatch):
    """handle_message only schedules the turn; the spool is the only copy until the input row lands,
    and is retired then (mid-turn), so a later restart never replays a prompt with tool effects."""
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="do the side effect", source=source, user_id=source.user_id, message_id="m-7")
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    recover_pending_to_db(Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
                          deferred_followup=partial(_defer_followup, runner, {}, None))
    spool, = (tmp_path / "pending_messages").glob("*.json")
    persist, release = asyncio.Event(), asyncio.Event()

    async def turn(recovered):
        await persist.wait()  # lease, transcript load, hygiene, agent build: no durable input yet
        runner.session_store.append_to_transcript(entry.session_id, {
            "role": "user", "content": recovered.text, "platform_message_id": "m-7"})
        await release.wait()

    async def handle(recovered):
        recovered._gateway_accepted = True
        adapter._session_tasks[entry.session_key] = asyncio.create_task(turn(recovered))

    adapter.handle_message = handle
    assert await runner._drain_startup_restore_queue() == 1
    for _ in range(5):
        await asyncio.sleep(0.05)
    assert spool.exists(), "a crash before the turn persisted its input would lose the follow-up"
    persist.set()
    for _ in range(40):
        if not spool.exists():
            break
        await asyncio.sleep(0.05)
    assert not spool.exists()

    # A second restart cuts the turn: the next boot must not replay the follow-up.
    observers = list(runner._background_tasks)
    for task in observers:
        task.cancel()
    await asyncio.gather(*observers, return_exceptions=True)
    owner = adapter._session_tasks.pop(entry.session_key)
    owner.cancel()
    await asyncio.gather(owner, return_exceptions=True)
    fresh_runner, _adapter, fresh_entry, _source = _setup(tmp_path, monkeypatch)
    assert recover_pending_to_db(
        Mock(), session_resolver=lambda *args, **kwargs: (fresh_entry.session_id, Mock()),
        deferred_followup=partial(_defer_followup, fresh_runner, {}, None)) == 0
    assert fresh_entry.session_key not in runner._restart_followup_keys


@pytest.mark.asyncio
async def test_followup_spool_whose_input_is_already_durable_without_resume_marker_is_declined(
        tmp_path, monkeypatch, caplog):
    """A durable platform id without an admitted resume marker is declined, not replayed."""
    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="do the side effect", source=source, user_id=source.user_id, message_id="m-8")
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    runner.session_store.append_to_transcript(entry.session_id, {
        "role": "user", "content": "do the side effect", "platform_message_id": "m-8"})
    with caplog.at_level("INFO", logger="gateway.run"):
        assert recover_pending_to_db(
            Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
            deferred_followup=partial(_defer_followup, runner, {}, None)) == 1
        assert await runner._drain_startup_restore_queue() == 0
        await asyncio.gather(*list(runner._background_tasks))
    assert runner._startup_restore_queue == []
    assert len(_adapter.sent) == 1
    assert "may already have been handled" in _adapter.sent[0]
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:possibly-duplicate:noted"]
    assert not list((tmp_path / "pending_messages").glob("*.json"))
    assert list((tmp_path / "pending_messages").glob("*.bad"))


def test_reconnect_followup_with_missing_origin_is_retained_without_attribute_error(tmp_path, monkeypatch):
    runner, _adapter, entry, _source = _setup(tmp_path, monkeypatch)
    entry.origin = None
    path = tmp_path / "pending.json"
    data = {"drain_deferred": True, "text": "saved", "source_user_id": "u1"}
    assert _defer_followup(
        runner, {entry.session_key: entry}, Platform.TELEGRAM, entry.session_key,
        entry.session_id, data, path, reconnect_recovery=True,
    ) is None




@pytest.mark.asyncio
async def test_durable_followup_with_fresh_admitted_resume_gets_hedged_note_and_quarantine(
        tmp_path, monkeypatch, caplog):
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    runner.session_store.mark_resume_pending(entry.session_key, turn_id="resume-now")
    event = MessageEvent(text="already resumed", source=source, user_id=source.user_id, message_id="fresh-1")
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    runner.session_store.append_to_transcript(entry.session_id, {
        "role": "user", "content": event.text, "platform_message_id": event.message_id,
    })
    with caplog.at_level("INFO", logger="gateway.run"):
        assert recover_pending_to_db(
            Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
            deferred_followup=partial(_defer_followup, runner, {}, None)) == 1
        assert await runner._drain_startup_restore_queue() == 0
        await asyncio.gather(*list(runner._background_tasks))
    assert len(adapter.sent) == 1
    assert "may already have been handled" in adapter.sent[0]
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:possibly-duplicate:noted"]
    assert not list((tmp_path / "pending_messages").glob("*.json"))
    assert list((tmp_path / "pending_messages").glob("*.bad"))


@pytest.mark.asyncio
async def test_durable_and_fresh_followups_same_session_do_not_silently_delete_durable_spool(
        tmp_path, monkeypatch, caplog):
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    runner.session_store.mark_resume_pending(entry.session_key, turn_id="resume-now")
    durable = MessageEvent(
        text="already handled", source=source, user_id=source.user_id, message_id="durable-1")
    fresh = MessageEvent(text="run this follow-up", source=source, user_id=source.user_id)
    assert flush_pending_to_file({entry.session_key: durable}, reason="shutdown_follow_up") == 1
    assert flush_pending_to_file({entry.session_key: fresh}, reason="shutdown_follow_up") == 1
    runner.session_store.append_to_transcript(entry.session_id, {
        "role": "user", "content": durable.text, "platform_message_id": durable.message_id,
    })

    claimed = []

    async def handle(event):
        claimed.append(event.text)
        event._gateway_accepted = True
        event._agent_turn_succeeded = True

    adapter.handle_message = handle
    with caplog.at_level("INFO", logger="gateway.run"):
        assert recover_pending_to_db(
            Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
            deferred_followup=partial(_defer_followup, runner, {}, None)) == 2
        # The fresh follow-up claims the session key, so the scheduler must skip its sibling;
        # the durable spool has no scheduler-dependent deletion path of its own.
        assert runner._restart_followup_keys == {entry.session_key}
        assert runner._schedule_resume_pending_sessions(candidates=[entry]) == 0
        # Releasing the declined sibling starts a background drain. Its accepted
        # successor need not contribute to this caller's batch count.
        await runner._drain_startup_restore_queue()
        await async_wait_until(
            lambda: len(_outcome_lines(caplog)) == 2,
            what="durable decline and fresh follow-up completion",
        )
        await runner._get_replay_scheduler().wait_idle()
    assert claimed == [fresh.text]
    assert len(adapter.sent) == 1 and "may already have been handled" in adapter.sent[0]
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:possibly-duplicate:noted",
        f"Restart recovery outcome: session={entry.session_key} outcome=resumed",
    ]
    assert not list((tmp_path / "pending_messages").glob("*.json"))
    assert len(list((tmp_path / "pending_messages").glob("*.bad"))) == 1


@pytest.mark.asyncio
async def test_durable_followup_with_expired_resume_marker_gets_hedged_note(tmp_path, monkeypatch, caplog):
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    runner.session_store.mark_resume_pending(entry.session_key, turn_id="resume-old")
    with runner.session_store._lock:
        runner.session_store._entries[entry.session_key].last_resume_marked_at = (
            datetime.now(UTC) - timedelta(hours=2))
    event = MessageEvent(text="already handled", source=source, user_id=source.user_id, message_id="expired-1")
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    runner.session_store.append_to_transcript(entry.session_id, {
        "role": "user", "content": event.text, "platform_message_id": event.message_id,
    })
    with caplog.at_level("INFO", logger="gateway.run"):
        assert recover_pending_to_db(
            Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
            deferred_followup=partial(_defer_followup, runner, {}, None)) == 1
        assert await runner._drain_startup_restore_queue() == 0
    assert len(adapter.sent) == 1 and "may already have been handled" in adapter.sent[0]
    assert "was not run" not in adapter.sent[0]
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:possibly-duplicate:noted"]
    assert list((tmp_path / "pending_messages").glob("*.bad"))


@pytest.mark.asyncio
async def test_stale_durable_followup_gets_hedged_note_not_not_run_notice(tmp_path, monkeypatch, caplog):
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="old but durable", source=source, user_id=source.user_id, message_id="stale-1")
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    spool, = (tmp_path / "pending_messages").glob("*.json")
    old = time.time() - 2 * 24 * 60 * 60
    os.utime(spool, (old, old))
    runner.session_store.append_to_transcript(entry.session_id, {
        "role": "user", "content": event.text, "platform_message_id": event.message_id,
    })
    with caplog.at_level("INFO", logger="gateway.run"):
        assert recover_pending_to_db(
            Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
            deferred_followup=partial(_defer_followup, runner, {}, None)) == 1
        assert await runner._drain_startup_restore_queue() == 0
    assert len(adapter.sent) == 1
    assert "may already have been handled" in adapter.sent[0]
    assert "was not run" not in adapter.sent[0]
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:possibly-duplicate:noted"]


@pytest.mark.asyncio
async def test_authorless_legacy_content_match_gets_hedged_note(tmp_path, monkeypatch, caplog):
    runner, adapter, entry, _source = _setup(tmp_path, monkeypatch)
    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir(parents=True, exist_ok=True)
    spool = flush_dir / "pending-authorless-legacy.json"
    spool_ts = time.time() - 10
    spool.write_text(json.dumps({
        "session_key": entry.session_key, "reason": "shutdown_follow_up", "ts": spool_ts, "seq": -1,
        "data": {"text": "authorless durable content"},
    }))
    runner.session_store.append_to_transcript(entry.session_id, {
        "role": "user", "content": "authorless durable content", "timestamp": time.time(),
    })
    with caplog.at_level("INFO", logger="gateway.run"):
        recover_pending_to_db(
            Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
            deferred_followup=partial(_defer_followup, runner, {}, None))
        assert await runner._drain_startup_restore_queue() == 0
    assert len(adapter.sent) == 1
    assert "may already have been handled" in adapter.sent[0]
    assert "was not run" not in adapter.sent[0]
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:possibly-duplicate:noted"]
    assert not spool.exists() and spool.with_name(spool.name + ".bad").exists()

def _outcome_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("Restart recovery outcome:")]


@pytest.mark.asyncio
async def test_draining_string_followup_is_spooled_with_authorship_then_recovered_and_run(
        tmp_path, monkeypatch, caplog):
    """A leftover /steer or interrupt is only a string; shutdown must still spool its author."""
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    runner._draining = True
    monkeypatch.setattr("gateway.run._dequeue_pending_event", lambda adapter, key: None)
    runner._promote_queued_event = lambda key, adapter, event: event
    event, text = await runner._run_agent_drain_pending(
        {"final_response": "done", "pending_steer": "the steered question"}, adapter, source, entry.session_key)
    assert event is None and text is None
    [spool] = list((tmp_path / "pending_messages").glob("*.json"))
    data = json.loads(spool.read_text())["data"]
    assert data["source_user_id"] == source.user_id
    assert data.get("internal") is False
    # The steer has no transport id of its own; the turn's message id must not be reused for it.
    assert data.get("message_id") is None and data["recovery_id"].startswith("hermes-recovery-")

    runner._draining = False
    db = Mock()
    assert recover_pending_to_db(
        db, session_resolver=lambda *args, **kwargs: (entry.session_id, db),
        deferred_followup=partial(_defer_followup, runner, {}, None)) == 1
    db.append_message.assert_not_called()
    called = []

    async def handle(recovered):
        called.append((recovered.text, recovered.source.user_id))
        recovered._agent_turn_succeeded = True

    adapter.set_message_handler(handle)
    with caplog.at_level("INFO", logger="gateway.run"):
        assert await runner._drain_startup_restore_queue() == 1
        await asyncio.gather(*list(adapter._background_tasks))
        await asyncio.gather(*list(runner._background_tasks))
    assert called == [("the steered question", source.user_id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", ["group", "thread"])
async def test_draining_string_followup_in_shared_session_is_unauthored_and_declined(
        tmp_path, monkeypatch, caplog, shared):
    """In a shared session another participant may have sent the leftover text, so the turn's
    author must not be stamped on it: recovery declines it visibly instead of running it."""
    runner, adapter, _entry, _source = _setup(tmp_path, monkeypatch)
    runner.config.group_sessions_per_user = shared != "group"
    runner.config.thread_sessions_per_user = False
    source = make_restart_source(chat_id="-100777", chat_type="group",
                                 thread_id="42" if shared == "thread" else None)
    entry = runner.session_store.get_or_create_session(source)
    runner._draining = True
    monkeypatch.setattr("gateway.run._dequeue_pending_event", lambda adapter, key: None)
    runner._promote_queued_event = lambda key, adapter, event: event
    event, text = await runner._run_agent_drain_pending(
        {"final_response": "done", "pending_steer": "someone else's steer"}, adapter, source, entry.session_key)
    assert event is None and text is None
    [spool] = list((tmp_path / "pending_messages").glob("*.json"))
    data = json.loads(spool.read_text())["data"]
    assert not data.get("source_user_id") and not data.get("user_id")

    runner._draining = False
    ran = []

    async def handle(recovered):
        ran.append(recovered.text)
        recovered._agent_turn_succeeded = True

    adapter.set_message_handler(handle)
    db = Mock()
    with caplog.at_level("INFO", logger="gateway.run"):
        recover_pending_to_db(
            db, session_resolver=lambda *args, **kwargs: (entry.session_id, db),
            deferred_followup=partial(_defer_followup, runner, {}, None))
        await runner._drain_startup_restore_queue()
        await asyncio.gather(*list(runner._background_tasks))
    db.append_message.assert_not_called()
    assert ran == []
    assert len(adapter.sent) == 1 and "send it again" in adapter.sent[0]
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:author-missing:noted"]
    assert not spool.exists()


@pytest.mark.asyncio
async def test_legacy_idless_duplicate_content_is_declined_not_deleted(tmp_path, monkeypatch, caplog):
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir(parents=True, exist_ok=True)
    spool = flush_dir / "pending-legacy.json"
    spool.write_text(json.dumps({
        "session_key": entry.session_key, "reason": "shutdown_follow_up", "ts": time.time() - 10, "seq": -1,
        "data": {"text": "continue", "source_user_id": source.user_id, "user_id": source.user_id},
    }))
    runner.session_store.append_to_transcript(entry.session_id, {
        "role": "user", "content": "continue", "platform_message_id": "later-message",
        "timestamp": time.time(),
    })
    with caplog.at_level("INFO", logger="gateway.run"):
        recover_pending_to_db(
            Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
            deferred_followup=partial(_defer_followup, runner, {}, None))
        await runner._drain_startup_restore_queue()
        await asyncio.gather(*list(runner._background_tasks))
    # It may already have run: the note must not claim it was not run or ask for a blind resend.
    assert len(adapter.sent) == 1
    assert "may already have been handled" in adapter.sent[0]
    assert "was not run" not in adapter.sent[0]
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:possibly-duplicate:noted"]
    assert not spool.exists()
    assert spool.with_name(spool.name + ".bad").exists()


@pytest.mark.asyncio
async def test_legacy_idless_duplicate_with_resume_pending_is_declined_not_dropped(tmp_path, monkeypatch, caplog):
    """A resume_pending marker does not prove a resume will run (it may be stale or not admitted),
    so a legacy content match still gets the hedged note and quarantine, never a silent drop."""
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    runner.session_store.mark_resume_pending(entry.session_key, turn_id="interrupted-turn")
    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir(parents=True, exist_ok=True)
    spool = flush_dir / "pending-legacy.json"
    spool.write_text(json.dumps({
        "session_key": entry.session_key, "reason": "shutdown_follow_up", "ts": time.time() - 10, "seq": -1,
        "data": {"text": "continue", "source_user_id": source.user_id, "user_id": source.user_id},
    }))
    runner.session_store.append_to_transcript(entry.session_id, {
        "role": "user", "content": "continue", "timestamp": time.time(),
    })
    with caplog.at_level("INFO", logger="gateway.run"):
        recover_pending_to_db(
            Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
            deferred_followup=partial(_defer_followup, runner, {}, None))
        await runner._drain_startup_restore_queue()
        await asyncio.gather(*list(runner._background_tasks))
    # A resume_pending marker does not prove a resume will run, so the durable input still gets
    # the hedged note and quarantine rather than a silent drop.
    assert runner._startup_restore_queue == []
    assert len(adapter.sent) == 1 and "may already have been handled" in adapter.sent[0]
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:possibly-duplicate:noted"]
    assert not spool.exists()
    assert spool.with_name(spool.name + ".bad").exists()


@pytest.mark.asyncio
async def test_noninternal_idless_event_gets_recovery_id_at_flush(tmp_path, monkeypatch):
    _runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="continue", source=source, user_id=source.user_id)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    payload = json.loads(next((tmp_path / "pending_messages").glob("*.json")).read_text())
    assert payload["data"].get("message_id") is None
    assert payload["data"]["recovery_id"].startswith("hermes-recovery-")


@pytest.mark.asyncio
async def test_legacy_text_only_shutdown_followup_gets_one_outcome_one_note_and_quarantine(
        tmp_path, monkeypatch, caplog):
    """The live stranded spool: data has only text, so it can never run, but it cannot stay silent."""
    runner, adapter, entry, _source = _setup(tmp_path, monkeypatch)
    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir(parents=True, exist_ok=True)
    spool = flush_dir / "pending-legacy.json"
    spool.write_text(json.dumps({
        "session_key": entry.session_key, "reason": "shutdown_follow_up", "ts": 1792018782, "seq": -1,
        "data": {"text": "a real question with no recorded author"}}))
    ran = []

    async def handle(event):
        ran.append(event.text)
        event._agent_turn_succeeded = True

    adapter.set_message_handler(handle)
    db = Mock()
    with caplog.at_level("INFO", logger="gateway.run"):
        for _boot in range(2):  # a second boot must not re-process or re-log it
            recover_pending_to_db(
                db, session_resolver=lambda *args, **kwargs: (entry.session_id, db),
                deferred_followup=partial(_defer_followup, runner, {}, None))
            await runner._drain_startup_restore_queue()
            await asyncio.gather(*list(runner._background_tasks))
    db.append_message.assert_not_called()
    assert ran == []
    assert len(adapter.sent) == 1 and "send it again" in adapter.sent[0]
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:author-missing:noted"]
    assert not spool.exists()
    assert (flush_dir / "pending-legacy.json.bad").exists()


@pytest.mark.asyncio
async def test_legacy_text_only_shutdown_followup_is_retained_while_note_cannot_be_sent(
        tmp_path, monkeypatch, caplog):
    runner, adapter, entry, _source = _setup(tmp_path, monkeypatch)
    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir(parents=True, exist_ok=True)
    spool = flush_dir / "pending-legacy.json"
    spool.write_text(json.dumps({
        "session_key": entry.session_key, "reason": "shutdown_follow_up", "ts": 1792018782, "seq": -1,
        "data": {"text": "a real question with no recorded author"}}))
    runner._delivery_adapter_for = lambda source: None
    runner._intake_adapter_for = lambda source: None
    db = Mock()
    with caplog.at_level("INFO", logger="gateway.run"):
        recover_pending_to_db(
            db, session_resolver=lambda *args, **kwargs: (entry.session_id, db),
            deferred_followup=partial(_defer_followup, runner, {}, None))
        await runner._drain_startup_restore_queue()
    db.append_message.assert_not_called()
    assert spool.exists()
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_stale_shutdown_followup_waits_for_offline_adapter_then_notes_and_quarantines(
        tmp_path, monkeypatch, caplog):
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="stale question", source=source, user_id=source.user_id)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    spool, = (tmp_path / "pending_messages").glob("*.json")
    old = time.time() - 2 * 24 * 60 * 60
    os.utime(spool, (old, old))
    runner._delivery_adapter_for = lambda source: None
    runner._intake_adapter_for = lambda source: None
    db = Mock()

    with caplog.at_level("INFO", logger="gateway.run"):
        recover_pending_to_db(
            db, session_resolver=lambda *args, **kwargs: (entry.session_id, db),
            deferred_followup=partial(_defer_followup, runner, {}, None),
        )
    assert spool.exists()
    assert _outcome_lines(caplog) == []

    runner._delivery_adapter_for = lambda source: adapter
    runner._intake_adapter_for = lambda source: adapter
    ran = []

    async def handle(recovered):
        ran.append(recovered.text)
        recovered._agent_turn_succeeded = True

    adapter.set_message_handler(handle)
    with caplog.at_level("INFO", logger="gateway.run"):
        assert recover_pending_to_db(
            db, session_resolver=lambda *args, **kwargs: (entry.session_id, db),
            deferred_followup=partial(_defer_followup, runner, {}, None),
        ) == 1
        assert await runner._drain_startup_restore_queue() == 0
        await asyncio.gather(*list(runner._background_tasks))

    assert ran == []
    assert len(adapter.sent) == 1
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:stale:noted"]
    assert not spool.exists()
    assert spool.with_name(spool.name + ".bad").exists()


@pytest.mark.parametrize("pruned", ["entry", "session-id"])
def test_40_day_stale_followup_for_pruned_session_is_dropped_once(tmp_path, monkeypatch, caplog, pruned):
    """A pruned session cannot ever receive the stale note; bound its spool retention."""
    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="stale question", source=source, user_id=source.user_id)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    spool, = (tmp_path / "pending_messages").glob("*.json")
    old = time.time() - 40 * 24 * 60 * 60
    os.utime(spool, (old, old))
    db = Mock()
    store = runner.session_store
    with store._lock:
        store._entries.pop(entry.session_key)
    resolver = ((lambda *args, **kwargs: (entry.session_id, db)) if pruned == "entry"
                else (lambda *args, **kwargs: None))
    deferred = partial(_defer_followup, runner, {}, None)
    if pruned == "entry":  # the reviewer's path: the deferral callback itself says "keep" (None)
        assert deferred(entry.session_key, entry.session_id, {
            "drain_deferred": True, "shutdown_follow_up": True}, spool) is None

    with caplog.at_level("INFO", logger="gateway.run"):
        assert recover_pending_to_db(db, session_resolver=resolver, deferred_followup=deferred) == 0
        # The next scan sees no spool and therefore cannot duplicate the decision.
        assert recover_pending_to_db(db, session_resolver=resolver, deferred_followup=deferred) == 0

    assert not spool.exists()
    assert runner._startup_restore_queue == []
    db.append_message.assert_not_called()
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:stale:note-unavailable"]


def test_six_day_stale_followup_for_pruned_session_is_still_retained(tmp_path, monkeypatch, caplog):
    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="stale question", source=source, user_id=source.user_id)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    spool, = (tmp_path / "pending_messages").glob("*.json")
    old = time.time() - 6 * 24 * 60 * 60
    os.utime(spool, (old, old))
    with runner.session_store._lock:
        runner.session_store._entries.pop(entry.session_key)
    db = Mock()
    with caplog.at_level("INFO", logger="gateway.run"):
        assert recover_pending_to_db(
            db, session_resolver=lambda *args, **kwargs: (entry.session_id, db),
            deferred_followup=partial(_defer_followup, runner, {}, None),
        ) == 0
    assert spool.exists()
    assert _outcome_lines(caplog) == []


def test_stale_shutdown_followup_drop_logs_note_unavailable_outcome_once(
        tmp_path, monkeypatch, caplog):
    _runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="stale question", source=source, user_id=source.user_id)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    spool, = (tmp_path / "pending_messages").glob("*.json")
    old = time.time() - 2 * 24 * 60 * 60
    os.utime(spool, (old, old))
    db = Mock()

    with caplog.at_level("INFO", logger="gateway.run"):
        assert recover_pending_to_db(
            db, session_resolver=lambda *args, **kwargs: (entry.session_id, db),
            deferred_followup=lambda *args: False,
        ) == 0

    assert not spool.exists()
    assert _outcome_lines(caplog) == [
        f"Restart recovery outcome: session={entry.session_key} outcome=skipped:stale:note-unavailable"]
