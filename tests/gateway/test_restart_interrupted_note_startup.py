"""S2 regressions: one durable visible note per restart-cut human turn."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import Platform
from gateway.run_startup import GatewayStartupMixin
from gateway.session import AsyncSessionStore

from tests.gateway.restart_note_support import (
    SwallowCancelNoteAdapter,
    _isolated_home as _isolated_home,
    _source,
    _store,
)


@pytest.mark.asyncio
async def test_reconnect_posts_missing_s2_note_before_resume(tmp_path, monkeypatch):
    """A platform offline during boot gets its durable note before reconnect resume."""
    from gateway import run_pending_recovery
    from tests.gateway.restart_test_helpers import make_restart_runner

    runner, adapter = make_restart_runner()
    store = _store(tmp_path)
    source = _source("reconnect-note")
    entry = store.get_or_create_session(source)
    store.mark_resume_pending(entry.session_key, turn_id="turn-reconnect", human=True)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._resume_pending_candidates = lambda platform=None, record_boot=False: [entry]
    runner._auto_resume_ready = lambda _entry: (adapter, source)
    runner._shutdown_notification_target = AsyncMock(
        return_value=(source, "telegram", source.chat_id, source.thread_id, None),
    )
    runner._delivery_adapter_for = lambda _source: adapter
    runner._authorization_adapter = lambda *_args: adapter
    runner._thread_metadata_for_target = lambda *args, **kwargs: {"thread_id": source.thread_id}
    runner._startup_restore_queue = []
    order = []
    original_send = adapter.send

    async def send_note(*args, **kwargs):
        order.append("note")
        return await original_send(*args, **kwargs)

    adapter.send = send_note

    def schedule_resume(**kwargs):
        order.append("resume")
        kwargs["restore_keys"].add(entry.session_key)
        return 1

    runner._schedule_resume_pending_sessions = schedule_resume
    monkeypatch.setattr(run_pending_recovery, "recover_pending_shutdown_flush", lambda *a, **k: None)

    await runner._recover_spool_after_reconnect(Platform.TELEGRAM)

    assert order == ["note", "resume"]
    assert len(adapter.sent) == 1
    assert store.get_restart_note(entry.session_key)[3] == "1"

@pytest.mark.asyncio
async def test_reconnect_detaches_cancel_ignoring_note_before_resume(tmp_path, monkeypatch):
    from gateway import run_pending_recovery
    from tests.gateway.restart_test_helpers import make_restart_runner

    runner, _ = make_restart_runner()
    store = _store(tmp_path)
    source = _source("detach-reconnect")
    entry = store.get_or_create_session(source)
    store.mark_resume_pending(entry.session_key, turn_id="turn-reconnect-detach", human=True)
    adapter = SwallowCancelNoteAdapter()
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._resume_pending_candidates = lambda platform=None, record_boot=False: [entry]
    runner._auto_resume_ready = lambda _entry: (adapter, source)
    runner._shutdown_notification_target = AsyncMock(
        return_value=(source, "telegram", source.chat_id, source.thread_id, None),
    )
    runner._delivery_adapter_for = lambda _source: adapter
    runner._authorization_adapter = lambda *_args: adapter
    runner._thread_metadata_for_target = lambda *args, **kwargs: {"thread_id": source.thread_id}
    runner._startup_restore_queue = []
    scheduled = []
    runner._schedule_resume_pending_sessions = lambda **kwargs: scheduled.append(kwargs["restore_keys"].add(entry.session_key))
    monkeypatch.setattr(run_pending_recovery, "recover_pending_shutdown_flush", lambda *a, **k: None)
    monkeypatch.setattr("gateway.run._startup_restore_drain_timeout_secs", lambda: 5.0)

    async def timeout_after_send_started(tasks, *, timeout=None, **kwargs):
        if timeout == 5.0:
            await adapter.started.wait()
            return set(), set(tasks)
        return await asyncio_wait(tasks, timeout=timeout, **kwargs)

    asyncio_wait = asyncio.wait
    monkeypatch.setattr("gateway.run_shutdown.asyncio.wait", timeout_after_send_started)

    returned = asyncio.Event()

    async def recover():
        try:
            await runner._recover_spool_after_reconnect(Platform.TELEGRAM)
        finally:
            returned.set()

    recovery_task = asyncio.create_task(recover())
    await asyncio.wait_for(adapter.started.wait(), timeout=1.0)
    timed_out = False
    try:
        await asyncio.wait_for(asyncio.shield(returned.wait()), timeout=0.9)
    except asyncio.TimeoutError:
        timed_out = True
    assert not timed_out
    adapter.release.set()
    await recovery_task
    await asyncio.sleep(0)

    assert not timed_out
    assert scheduled == [None]
    assert adapter.send_calls == 1
    assert store.get_restart_note(entry.session_key)[3].startswith("sending:")

@pytest.mark.asyncio
async def test_startup_detaches_cancel_ignoring_note_before_resume(monkeypatch):
    import gateway.run as run_module

    runner = object.__new__(GatewayStartupMixin)
    started = asyncio.Event()
    release = asyncio.Event()
    scheduled = []

    async def hanging_note(*_args, **kwargs):
        started.set()
        try:
            await asyncio.wait_for(release.wait(), timeout=kwargs["timeout"])
        except asyncio.TimeoutError:
            return 0

    runner._claim_pending_obligations = AsyncMock(return_value=[])
    runner._send_interrupted_turn_notes = hanging_note
    runner._send_restart_notification = AsyncMock()
    runner._redeliver_claimed_obligations = AsyncMock()
    monkeypatch.setattr(run_module, "_startup_restore_drain_timeout_secs", lambda: 0.1)

    returned = asyncio.Event()

    async def await_boot():
        try:
            await runner._await_startup_boot_sends(
                planned_restart_notification_pending=False,
                interrupted_note_keys=["startup-detach"],
            )
        finally:
            returned.set()

    boot_task = asyncio.create_task(await_boot())
    await asyncio.wait_for(started.wait(), timeout=1.0)
    timed_out = False
    try:
        await asyncio.wait_for(asyncio.shield(returned.wait()), timeout=0.9)
    except asyncio.TimeoutError:
        timed_out = True
    assert not timed_out
    release.set()
    await boot_task
    scheduled.append("resume")

    assert not timed_out
    assert scheduled == ["resume"]

def test_startup_note_candidates_reject_legacy_and_stale_rows():
    from gateway.run_startup import GatewayStartupMixin

    runner = object.__new__(GatewayStartupMixin)
    runner._auto_resume_ready = lambda entry: object() if entry.session_key == "fresh" else None
    candidates = [
        SimpleNamespace(session_key="legacy", resume_turn_id=None),
        SimpleNamespace(session_key="stale", resume_turn_id="turn-stale"),
        SimpleNamespace(session_key="fresh", resume_turn_id="turn-fresh"),
    ]

    assert runner._startup_interrupted_note_candidates(candidates) == ["fresh"]

@pytest.mark.asyncio
async def test_startup_boot_send_path_posts_interrupted_notes_first(monkeypatch):
    from gateway.run_startup import GatewayStartupMixin
    import gateway.run as run_module

    runner = object.__new__(GatewayStartupMixin)
    calls = []
    runner._claim_pending_obligations = AsyncMock(return_value=[])
    runner._send_restart_notification = AsyncMock(side_effect=lambda: calls.append("restart"))
    runner._schedule_update_notification_watch = lambda: calls.append("update-watch")
    runner._redeliver_claimed_obligations = AsyncMock(side_effect=lambda _rows: calls.append("redeliver"))
    runner._send_interrupted_turn_notes = AsyncMock(
        side_effect=lambda keys, **kwargs: calls.append((keys, kwargs)),
    )
    runner._retain_background_task = lambda task: None
    runner._late_failure_callback = lambda *args, **kwargs: (lambda task: None)
    monkeypatch.setattr(run_module, "_startup_restore_drain_timeout_secs", lambda: 0)

    await runner._await_startup_boot_sends(
        planned_restart_notification_pending=False,
        interrupted_note_keys=["fresh"],
    )

    assert calls == [
        (["fresh"], {
            "reclaim_pending": True,
            "cancel_on_timeout": True,
            "timeout": 0,
        }),
        "restart",
        "update-watch",
        "redeliver",
    ]

@pytest.mark.asyncio
async def test_startup_claims_ledger_answer_before_resume_snapshot(tmp_path, monkeypatch):
    """An answered ledger row must not be resumed or get a startup interruption note (#91969)."""
    from gateway import delivery_ledger as ledger
    from gateway.run_startup import GatewayStartupMixin
    import gateway.run_pending_recovery as pending_recovery
    import gateway.run_startup as startup_module
    import gateway.run as run_module

    monkeypatch.setattr(ledger, "_db_path", lambda: tmp_path / "state.db")
    obligation_id = ledger.compute_obligation_id("answered-session", "inbound", "answer")
    ledger.record_obligation(
        obligation_id=obligation_id, session_key="answered-session", platform="telegram",
        chat_id="chat", thread_id="thread", content="answer",
    )
    ledger.mark_attempting(obligation_id)
    with ledger._connect() as conn:
        conn.execute(
            "UPDATE delivery_obligations SET owner_pid=?, owner_started_at=? WHERE obligation_id=?",
            (999999999, 1, obligation_id),
        )

    store = _store(tmp_path / "sessions")
    source = _source("startup-order")
    entry = store.get_or_create_session(source)
    entry.session_key = "answered-session"
    store._entries["answered-session"] = entry
    store.mark_resume_pending("answered-session", turn_id="turn-answered", human=True)

    runner = object.__new__(GatewayStartupMixin)
    runner.session_store = store
    runner.async_session_store = AsyncSessionStore(store)
    runner.adapters = {Platform.TELEGRAM: object()}
    runner._profile_adapters = {}
    runner._restart_loop_guard_config = lambda: (100, 3600, 0)
    runner._auto_resume_ready = lambda _entry: (object(), source)
    runner._start_post_connect_services = AsyncMock()
    runner._send_restart_notification = AsyncMock()
    runner._schedule_update_notification_watch = Mock()
    runner._redeliver_claimed_obligations = AsyncMock()
    note_calls = []
    runner._send_interrupted_turn_notes = AsyncMock(
        side_effect=lambda keys, **kwargs: note_calls.append((keys, kwargs)),
    )
    scheduled = []
    runner._schedule_resume_pending_sessions = Mock(
        side_effect=lambda **kwargs: scheduled.append(kwargs["candidates"]),
    )
    runner._finish_startup_restore = AsyncMock()
    runner._schedule_auto_resume_delegations = Mock()
    runner._send_session_db_warning_notifications = AsyncMock()
    monkeypatch.setattr(run_module, "_startup_restore_drain_timeout_secs", lambda: 0)
    monkeypatch.setattr(startup_module, "recover_pending_shutdown_flush", lambda *args, **kwargs: None,
                        raising=False)
    monkeypatch.setattr(pending_recovery, "recover_pending_shutdown_flush", lambda *args, **kwargs: None)
    monkeypatch.setattr("gateway.restart_loop_guard.check_and_record", lambda *args, **kwargs: False)

    await GatewayStartupMixin._start_finish_wiring(runner, 0)

    assert entry.resume_pending is False
    assert note_calls == []
    assert scheduled == [[]]

@pytest.mark.asyncio
async def test_startup_timeout_cancels_note_before_resume(tmp_path, monkeypatch):
    runner = object.__new__(GatewayStartupMixin)
    events = []
    blocker = asyncio.Event()

    async def hanging_notes(_keys, **kwargs):
        events.append("note-start")
        try:
            await asyncio.wait_for(blocker.wait(), timeout=kwargs["timeout"])
        except asyncio.TimeoutError:
            events.append("note-cancelled")
            return 0

    runner._claim_pending_obligations = AsyncMock(return_value=[])
    runner._send_interrupted_turn_notes = hanging_notes
    runner._send_restart_notification = AsyncMock()
    runner._schedule_update_notification_watch = Mock()
    runner._replay_pending_planned_restart_notification = AsyncMock()
    runner._redeliver_claimed_obligations = AsyncMock()
    monkeypatch.setattr("gateway.run._startup_restore_drain_timeout_secs", lambda: 0.01)

    await runner._await_startup_boot_sends(
        planned_restart_notification_pending=False,
        interrupted_note_keys=["session-key"],
    )
    events.append("resume")
    await asyncio.sleep(0)

    assert events == ["note-start", "note-cancelled", "resume"]
    blocker.set()

@pytest.mark.asyncio
async def test_startup_note_timeout_does_not_cancel_other_boot_sends(monkeypatch):
    """Only the S2 note step is cancel-on-timeout; boot sends keep running detached."""
    import gateway.run as run_module

    runner = object.__new__(GatewayStartupMixin)
    runner._background_tasks = set()
    runner._wait_bounded_or_release = GatewayStartupMixin._wait_bounded_or_release.__get__(runner)
    runner._late_failure_callback = lambda *args, **kwargs: (lambda task: None)
    runner._retain_background_task = lambda task: (runner._background_tasks.add(task), task)[1]
    events = []
    release_redelivery = asyncio.Event()
    note_started = asyncio.Event()

    async def notes(_keys, **kwargs):
        note_started.set()
        events.append(("note", kwargs["cancel_on_timeout"], kwargs["timeout"]))
        try:
            await asyncio.wait_for(asyncio.Event().wait(), timeout=kwargs["timeout"])
        except asyncio.TimeoutError:
            events.append("note-cancelled")
            return 0

    async def redeliver(_rows):
        events.append("redeliver-start")
        try:
            await release_redelivery.wait()
        except asyncio.CancelledError:
            events.append("redeliver-cancelled")
            raise
        events.append("redeliver-done")
        return 1

    runner._claim_pending_obligations = AsyncMock(return_value=[{"obligation_id": "owed"}])
    runner._send_interrupted_turn_notes = notes
    runner._send_restart_notification = AsyncMock(side_effect=lambda: events.append("restart"))
    runner._schedule_update_notification_watch = Mock(side_effect=lambda: events.append("update-watch"))
    runner._redeliver_claimed_obligations = redeliver
    monkeypatch.setattr(run_module, "_startup_restore_drain_timeout_secs", lambda: 0.01)

    await runner._await_startup_boot_sends(
        planned_restart_notification_pending=False,
        interrupted_note_keys=["interrupted"],
    )

    assert note_started.is_set()
    assert events[:4] == [
        ("note", True, 0.01),
        "note-cancelled",
        "restart",
        "update-watch",
    ]
    assert "redeliver-start" in events
    assert "redeliver-done" not in events
    redelivery_tasks = [task for task in runner._background_tasks if not task.done()]
    assert len(redelivery_tasks) == 1
    assert not redelivery_tasks[0].cancelled()

    release_redelivery.set()
    await asyncio.gather(*redelivery_tasks)
    assert events[-1] == "redeliver-done"
    assert "redeliver-cancelled" not in events

@pytest.mark.asyncio
async def test_startup_slow_note_finishes_before_detached_boot_sends_and_resume(monkeypatch):
    """A timed-out note is settled before resume scheduling while other sends remain live."""
    import gateway.run as run_module

    runner = object.__new__(GatewayStartupMixin)
    runner._background_tasks = set()
    runner._wait_bounded_or_release = GatewayStartupMixin._wait_bounded_or_release.__get__(runner)
    runner._late_failure_callback = lambda *args, **kwargs: (lambda task: None)
    runner._retain_background_task = lambda task: (runner._background_tasks.add(task), task)[1]
    events = []
    release_redelivery = asyncio.Event()

    async def notes(_keys, **kwargs):
        events.append("note-start")
        try:
            await asyncio.wait_for(asyncio.Event().wait(), timeout=kwargs["timeout"])
        except asyncio.TimeoutError:
            events.append("note-cancelled")
            return 0

    async def redeliver(_rows):
        events.append("redeliver-start")
        await release_redelivery.wait()
        events.append("redeliver-done")

    runner._claim_pending_obligations = AsyncMock(return_value=[])
    runner._send_interrupted_turn_notes = notes
    runner._send_restart_notification = AsyncMock(side_effect=lambda: events.append("restart"))
    runner._schedule_update_notification_watch = Mock()
    runner._redeliver_claimed_obligations = redeliver
    monkeypatch.setattr(run_module, "_startup_restore_drain_timeout_secs", lambda: 0.01)

    await runner._await_startup_boot_sends(
        planned_restart_notification_pending=False,
        interrupted_note_keys=["interrupted"],
    )
    events.append("resume")

    assert events[:4] == ["note-start", "note-cancelled", "restart", "redeliver-start"]
    assert events[-1] == "resume"
    assert "redeliver-done" not in events

    release_redelivery.set()
    await asyncio.gather(*runner._background_tasks)
    assert events[-1] == "redeliver-done"

@pytest.mark.asyncio
async def test_reconnect_timeout_cancels_note_before_resume(tmp_path, monkeypatch):
    from gateway import run_pending_recovery
    from tests.gateway.restart_test_helpers import make_restart_runner

    runner, _adapter = make_restart_runner()
    store = _store(tmp_path)
    source = _source("reconnect-timeout")
    entry = store.get_or_create_session(source)
    store.mark_resume_pending(entry.session_key, turn_id="turn-reconnect-timeout", human=True)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._resume_pending_candidates = lambda platform=None, record_boot=False: [entry]
    runner._auto_resume_ready = lambda _entry: (object(), source)
    runner._startup_restore_queue = []
    runner._drain_startup_restore_queue = AsyncMock()
    monkeypatch.setattr(run_pending_recovery, "recover_pending_shutdown_flush", lambda *a, **k: None)
    monkeypatch.setattr("gateway.run._startup_restore_drain_timeout_secs", lambda: 0.01)

    events = []
    blocker = asyncio.Event()
    detached = []

    async def late_note():
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            events.append("note-cancelled")
            raise
        events.append("note-delivered")

    async def notes(_keys, **kwargs):
        events.append("note-start")
        task = asyncio.create_task(late_note())
        if not kwargs.get("cancel_on_timeout"):
            detached.append(task)
            return 0
        try:
            await asyncio.wait_for(task, timeout=kwargs["timeout"])
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return 0

    runner._send_interrupted_turn_notes = notes

    def schedule_resume(**kwargs):
        events.append("resume")
        kwargs["restore_keys"].add(entry.session_key)

    runner._schedule_resume_pending_sessions = schedule_resume

    await runner._recover_spool_after_reconnect(Platform.TELEGRAM)
    blocker.set()
    await asyncio.sleep(0)

    assert events == ["note-start", "note-cancelled", "resume"]
    for task in detached:
        task.cancel()
    if detached:
        await asyncio.gather(*detached, return_exceptions=True)
