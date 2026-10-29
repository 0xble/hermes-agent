"""Recovery decisions must describe completed turns or acknowledged visible notes, never mere dispatch."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig
from gateway.platforms.event import MessageEvent
from gateway.session import SessionStore
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source
from tests.gateway.test_restart_interrupted_note import NoteAdapter


def _runner(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "managed"))
    runner, _ = make_restart_runner()
    source = make_restart_source()
    store = SessionStore(tmp_path, GatewayConfig(restart_resume_policy="continue"))
    entry = store.get_or_create_session(source)
    store.mark_resume_pending(entry.session_key, turn_id="cut-turn", human=True)
    runner.session_store = store
    adapter = NoteAdapter()
    runner.adapters = {source.platform: adapter}
    runner._delivery_adapter_for = lambda _source: adapter
    runner._authorization_adapter = lambda *_args: adapter
    runner._shutdown_notification_target = AsyncMock(return_value=(
        source, "telegram", source.chat_id, source.thread_id, None,
    ))
    runner._thread_metadata_for_target = lambda *args, **kwargs: {}
    runner._persist_active_agents = Mock()
    runner._startup_restore_tasks = []
    runner._startup_restore_queue = []
    runner._startup_restore_in_progress = True
    runner._is_user_authorized_for_source = lambda _source: True
    runner._restart_loop_guard_config = lambda: (1, 60, 300)
    monkeypatch.setattr("gateway.run._restart_notification_pending", lambda: False)
    monkeypatch.setattr("gateway.run._planned_restart_notification_pending", lambda: False)
    monkeypatch.setattr("gateway.restart_loop_guard._state_path", lambda: tmp_path / "breaker.json")
    return runner, adapter, entry


def _outcomes(caplog):
    return [record.getMessage() for record in caplog.records
            if record.getMessage().startswith("Restart recovery outcome:")]


def _line(entry, outcome):
    return f"Restart recovery outcome: session={entry.session_key} outcome={outcome}"


@pytest.mark.asyncio
async def test_resume_outcome_is_logged_once_only_after_turn_finishes(tmp_path, monkeypatch, caplog):
    runner, adapter, entry = _runner(tmp_path, monkeypatch)
    release = asyncio.Event()
    entered = asyncio.Event()

    async def turn():
        entered.set()
        await release.wait()
        runner.session_store.clear_resume_pending(entry.session_key)

    async def handle(event):
        event._gateway_accepted = True
        adapter._session_tasks[entry.session_key] = asyncio.create_task(turn())

    adapter.handle_message = handle
    monkeypatch.setattr("gateway.run_startup._STARTUP_RESUME_OPENING_S", 0.01)
    with caplog.at_level("INFO", logger="gateway.run"):
        assert runner._schedule_resume_pending_sessions(candidates=[entry], start_scheduler=True) == 1
        await entered.wait()
        await asyncio.sleep(0.02)
        assert _outcomes(caplog) == []
        release.set()
        await asyncio.gather(*runner._startup_restore_tasks)
        # A reconnect scan cannot create a second terminal outcome for this boot.
        runner._schedule_resume_pending_sessions(candidates=[entry], start_scheduler=True)
    assert _outcomes(caplog) == [_line(entry, "resumed")]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["declined", "raised"])
async def test_nonadmitted_resume_gets_a_visible_note_not_a_false_resumed_log(tmp_path, monkeypatch, caplog, failure):
    runner, adapter, entry = _runner(tmp_path, monkeypatch)

    async def handle(event):
        if failure == "raised":
            raise RuntimeError("adapter dispatch failed before handoff")
        # Real adapters return normally when no handler / routed identity admission is declined.

    adapter.handle_message = handle
    with caplog.at_level("INFO", logger="gateway.run"):
        assert runner._schedule_resume_pending_sessions(candidates=[entry], start_scheduler=True) == 1
        results = await asyncio.gather(*runner._startup_restore_tasks, return_exceptions=True)
        assert isinstance(results[0], RuntimeError) if failure == "raised" else results == [None]
    assert len(adapter.sent) == 1
    assert _outcomes(caplog) == [_line(entry, "noted")]
    assert entry.resume_pending
    assert not runner._is_session_running(entry.session_key)


@pytest.mark.asyncio
async def test_accepted_resume_without_session_task_is_not_reported_as_resumed(tmp_path, monkeypatch, caplog):
    runner, adapter, entry = _runner(tmp_path, monkeypatch)

    async def handle(event):
        event._gateway_accepted = True

    adapter.handle_message = handle
    with caplog.at_level("INFO", logger="gateway.run"):
        assert runner._schedule_resume_pending_sessions(candidates=[entry], start_scheduler=True) == 1
        await asyncio.gather(*runner._startup_restore_tasks)
    assert _outcomes(caplog) == [_line(entry, "noted")]
    assert entry.resume_pending


@pytest.mark.asyncio
async def test_visible_note_receipt_is_retained_without_shutdown_initialized_sets(tmp_path, monkeypatch):
    runner, adapter, entry = _runner(tmp_path, monkeypatch)
    assert await runner._send_interrupted_turn_notes([entry.session_key]) == 1
    assert entry.session_key in runner._s2_note_delivered_keys
    # A new boot must reconcile the durable message receipt without sending a duplicate.
    del runner._s2_note_delivered_keys
    runner._s2_note_claimed_keys.clear()
    assert await runner._send_interrupted_turn_notes([entry.session_key], reclaim_pending=True) == 0
    assert entry.session_key in runner._s2_note_delivered_keys
    assert len(adapter.sent) == 1


@pytest.mark.asyncio
async def test_pending_note_is_not_misreported_as_visible(tmp_path, monkeypatch):
    runner, adapter, entry = _runner(tmp_path, monkeypatch)
    assert runner.session_store.claim_restart_note(entry.session_key, expected_marker=runner.session_store.get_resume_pending_marker(entry.session_key))
    assert await runner._send_interrupted_turn_notes([entry.session_key]) == 0
    assert entry.session_key not in getattr(runner, "_s2_note_delivered_keys", ())
    assert adapter.sent == []


def test_breaker_preserves_pending_snapshot_for_note_fallback(tmp_path, monkeypatch):
    runner, _adapter, entry = _runner(tmp_path, monkeypatch)
    candidates = runner._resume_pending_candidates()
    assert candidates == [entry]
    assert candidates.breaker_tripped is True
    assert entry.resume_pending


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_breaker_decision_survives_refresh_and_reconnect(tmp_path, monkeypatch, caplog, ambiguous):
    runner, adapter, entry = _runner(tmp_path, monkeypatch)
    if ambiguous:
        assert runner.session_store.claim_restart_note(entry.session_key, expected_marker=runner.session_store.get_resume_pending_marker(entry.session_key))
        assert runner.session_store.mark_restart_note_sending(entry.session_key, expected_marker=runner.session_store.get_resume_pending_marker(entry.session_key))
    adapter.handle_message = AsyncMock()
    runner._start_post_connect_services = AsyncMock()
    runner._claim_pending_obligations = AsyncMock(return_value=[])
    runner._send_restart_notification = AsyncMock()
    runner._schedule_update_notification_watch = Mock()
    runner._redeliver_claimed_obligations = AsyncMock()
    runner._await_mcp_discovery = AsyncMock()
    runner._schedule_auto_resume_delegations = Mock()
    runner._send_session_db_warning_notifications = AsyncMock()
    monkeypatch.setattr("tools.process_registry.process_registry.pending_watchers", [])
    # No real cron-store subscriptions are necessary for this boot-path recovery contract.
    monkeypatch.setattr("gateway.cron_store_notices.install_cron_store_notices", lambda *_: None)
    with caplog.at_level("INFO", logger="gateway.run"):
        await asyncio.wait_for(runner._start_finish_wiring(0), 5)
        await runner._recover_spool_after_reconnect(entry.origin.platform)
    adapter.handle_message.assert_not_awaited()
    assert len(adapter.sent) == (0 if ambiguous else 1)
    outcome = "skipped:restart-loop-breaker:ambiguous-note" if ambiguous else "noted"
    assert _outcomes(caplog) == [_line(entry, outcome)]
    assert entry.resume_pending  # retained for a real user wake, never blindly replay work


@pytest.mark.asyncio
async def test_ambiguous_note_prefers_eligible_resume_without_resending_note(tmp_path, monkeypatch, caplog):
    runner, adapter, entry = _runner(tmp_path, monkeypatch)
    assert runner.session_store.claim_restart_note(entry.session_key, expected_marker=runner.session_store.get_resume_pending_marker(entry.session_key))
    assert runner.session_store.mark_restart_note_sending(entry.session_key, expected_marker=runner.session_store.get_resume_pending_marker(entry.session_key))
    assert await runner._send_interrupted_turn_notes([entry.session_key], reclaim_pending=True) == 0

    async def handle(event):
        event._gateway_accepted = True
        runner.session_store.clear_resume_pending(entry.session_key)

    adapter.handle_message = handle
    with caplog.at_level("INFO", logger="gateway.run"):
        assert runner._schedule_resume_pending_sessions(candidates=[entry], start_scheduler=True) == 1
        await asyncio.gather(*runner._startup_restore_tasks)
    assert adapter.sent == []
    assert _outcomes(caplog) == [_line(entry, "resumed")]


@pytest.mark.parametrize("reason", ["stale", "unauthorized", "adapter-offline", "suspended", "missing-origin"])
def test_resume_admission_skips_are_observable_once(tmp_path, monkeypatch, caplog, reason):
    runner, _adapter, entry = _runner(tmp_path, monkeypatch)
    mutations = {
        "stale": lambda: setattr(entry, "last_resume_marked_at", datetime.now() - timedelta(hours=3)),
        "unauthorized": lambda: setattr(runner, "_is_user_authorized_for_source", lambda _source: False),
        "adapter-offline": lambda: setattr(runner, "_delivery_adapter_for", lambda _source: None),
        "suspended": lambda: setattr(entry, "suspended", True),
        "missing-origin": lambda: setattr(entry, "origin", None),
    }
    mutations[reason]()
    with caplog.at_level("INFO", logger="gateway.run"):
        assert runner._schedule_resume_pending_sessions(candidates=[entry], start_scheduler=True) == 0
        assert runner._schedule_resume_pending_sessions(candidates=[entry], start_scheduler=True) == 0
    assert _outcomes(caplog) == [_line(entry, f"skipped:{reason}")]
    assert entry.resume_pending
