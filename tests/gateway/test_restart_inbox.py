"""Crash-durable inbound queue for messages accepted during restart drain."""

import asyncio
import sqlite3
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource, SessionStore
from gateway import restart_inbox as inbox
from gateway.run import GatewayRunner
from tests.gateway.restart_test_helpers import make_restart_runner as _make_restart_runner


def make_restart_runner():
    from hermes_state import SessionDB
    runner, adapter = _make_restart_runner()
    home = inbox._db_path().parent
    runner.session_store = SessionStore(sessions_dir=home / "sessions", config=runner.config)
    runner.session_store._routing_home = home
    runner.session_store._db = SessionDB(db_path=inbox._db_path())
    return runner, adapter


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(inbox, "_db_path", lambda: home / "state.db")


def _event(message_id="msg-1", text="continue this work"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="2027045491",
            chat_type="dm",
            user_id="2027045491",
            user_name="Brian",
            thread_id="165161",
            profile="default",
        ),
        user_id="2027045491",
        user_name="Brian",
        message_id=message_id,
        platform_update_id=991,
        reply_to_message_id="prior-1",
        reply_to_text="prior text",
        media_urls=["/tmp/example.png"],
        media_types=["image/png"],
        metadata={"safe": "value"},
    )


def _orphan(queue_id):
    with sqlite3.connect(inbox._db_path()) as conn:
        conn.execute(
            "UPDATE restart_inbox SET owner_pid=999999999, owner_started_at=1 "
            "WHERE queue_id=?",
            (queue_id,),
        )


def test_legacy_column_migration_tolerates_concurrent_winner(monkeypatch):
    path = inbox._db_path()
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE restart_inbox (
                queue_id TEXT PRIMARY KEY,
                session_key TEXT NOT NULL,
                platform TEXT NOT NULL,
                adapter_profile TEXT NOT NULL DEFAULT 'default',
                event_json TEXT NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                owner_pid INTEGER,
                owner_started_at INTEGER,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )

    real_connect = sqlite3.connect
    raced = False

    class RacingConnection:
        def __init__(self, connection):
            object.__setattr__(self, "connection", connection)

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def __setattr__(self, name, value):
            setattr(self.connection, name, value)

        def execute(self, sql, *args, **kwargs):
            nonlocal raced
            if not raced and sql.startswith("ALTER TABLE restart_inbox ADD COLUMN protocol"):
                raced = True
                self.connection.execute(sql, *args, **kwargs)
                raise sqlite3.OperationalError("duplicate column name: protocol")
            return self.connection.execute(sql, *args, **kwargs)

    monkeypatch.setattr(
        inbox.sqlite3,
        "connect",
        lambda *args, **kwargs: RacingConnection(real_connect(*args, **kwargs)),
    )

    conn = inbox._connect(path)
    conn.close()

    with real_connect(path) as check:
        columns = {row[1] for row in check.execute("PRAGMA table_info(restart_inbox)")}
    assert raced is True
    assert {"protocol", "input_owner"} <= columns


def test_round_trip_preserves_normalized_event_without_raw_platform_object():
    original = _event()
    payload = inbox.serialize_event(original)
    recovered = inbox.deserialize_event(payload)

    assert recovered.text == original.text
    assert recovered.message_id == original.message_id
    assert recovered.platform_update_id == original.platform_update_id
    assert recovered.source.platform == Platform.TELEGRAM
    assert recovered.source.thread_id == "165161"
    assert recovered.media_urls == ["/tmp/example.png"]
    assert recovered.metadata == {"safe": "value"}
    assert recovered.raw_message is None


def test_record_is_idempotent_by_platform_message_identity():
    event = _event()
    first = inbox.record_event("session-key", event, adapter_profile="default")
    second = inbox.record_event("session-key", event, adapter_profile="default")

    assert first == second
    # Only a replayed event gets the lifecycle marker.  Stamping the live
    # drain-accepting event would let its normal success finalizer consume the
    # pending row before process replacement.
    assert not hasattr(event, "_restart_inbox_queue_id")
    with sqlite3.connect(inbox._db_path()) as conn:
        assert conn.execute("SELECT count(*) FROM restart_inbox").fetchone()[0] == 1
        assert conn.execute(
            "SELECT state FROM restart_inbox WHERE queue_id=?", (first,)
        ).fetchone()[0] == "pending"


def test_dead_owner_event_is_claimed_once_and_handoff_fences_replay():
    queue_id = inbox.record_event("session-key", _event(), adapter_profile="default")
    _orphan(queue_id)

    claimed = inbox.claim_recoverable(
        deliverable_targets={("telegram", "default")}
    )

    assert [row["queue_id"] for row in claimed] == [queue_id]
    assert inbox.claim_recoverable(
        deliverable_targets={("telegram", "default")}
    ) == []

    assert inbox.mark_handed_off(queue_id) is True
    _orphan(queue_id)
    assert inbox.claim_recoverable(
        deliverable_targets={("telegram", "default")}
    ) == []


def test_live_owner_reserves_session_from_later_orphan_claim(monkeypatch):
    first_id = inbox.record_event(
        "session-key", _event(message_id="first"), adapter_profile="default"
    )
    second_id = inbox.record_event(
        "session-key", _event(message_id="second"), adapter_profile="default"
    )
    with sqlite3.connect(inbox._db_path()) as conn:
        conn.execute(
            "UPDATE restart_inbox SET created_at=100, owner_pid=101, owner_started_at=1 "
            "WHERE queue_id=?",
            (first_id,),
        )
        conn.execute(
            "UPDATE restart_inbox SET created_at=101, owner_pid=202, owner_started_at=2 "
            "WHERE queue_id=?",
            (second_id,),
        )
    monkeypatch.setattr(inbox.time, "time", lambda: 102)
    monkeypatch.setattr(
        inbox, "_owner_alive", lambda pid, started: (pid, started) == (101, 1)
    )

    assert inbox.claim_recoverable(
        deliverable_targets={("telegram", "default")}
    ) == []

    rows = {row["queue_id"]: row for row in inbox.read_rows(inbox._db_path())}
    assert rows[first_id]["state"] == "pending"
    assert rows[second_id]["state"] == "pending"
    assert rows[second_id]["attempts"] == 0


def test_failed_dispatch_releases_claim_for_retry():
    queue_id = inbox.record_event("session-key", _event())
    _orphan(queue_id)
    assert inbox.claim_recoverable(
        deliverable_targets={("telegram", "default")}
    )

    assert inbox.release_claim(queue_id)
    assert inbox.claim_recoverable(
        deliverable_targets={("telegram", "default")}
    )


def test_unavailable_adapter_does_not_spend_claim():
    queue_id = inbox.record_event("session-key", _event(), adapter_profile="default")
    _orphan(queue_id)

    assert inbox.claim_recoverable(
        deliverable_targets={("slack", "default")}
    ) == []

    with sqlite3.connect(inbox._db_path()) as conn:
        state, attempts = conn.execute(
            "SELECT state, attempts FROM restart_inbox WHERE queue_id=?",
            (queue_id,),
        ).fetchone()
    assert state == "pending"
    assert attempts == 0


@pytest.mark.asyncio
async def test_gateway_replays_durable_inbound_through_the_live_adapter():
    queue_id = inbox.record_event(
        "session-key", _event(text="continue the task"), adapter_profile="default"
    )
    _orphan(queue_id)
    runner, adapter = make_restart_runner()
    adapter.handle_message = AsyncMock()

    count = await GatewayRunner._drain_restart_inbox(runner)

    assert count == 1
    adapter.handle_message.assert_awaited_once()
    replay = adapter.handle_message.await_args_list[0].args[0]
    assert replay.text == "continue the task"
    assert getattr(replay, "_restart_inbox_queue_id") == queue_id
    with sqlite3.connect(inbox._db_path()) as conn:
        state = conn.execute(
            "SELECT state FROM restart_inbox WHERE queue_id=?", (queue_id,)
        ).fetchone()[0]
    assert state == "delivered"


@pytest.mark.asyncio
async def test_gateway_keeps_claim_when_replay_hands_off_to_an_agent_task():
    queue_id = inbox.record_event(
        "session-key", _event(text="continue the task"), adapter_profile="default"
    )
    _orphan(queue_id)
    runner, adapter = make_restart_runner()
    adapter._session_tasks = {}

    async def hand_off(event):
        task = asyncio.current_task()
        assert task is not None
        adapter._session_tasks["session-key"] = task

    adapter.handle_message = hand_off

    count = await GatewayRunner._drain_restart_inbox(runner)

    assert count == 1
    with sqlite3.connect(inbox._db_path()) as conn:
        state = conn.execute(
            "SELECT state FROM restart_inbox WHERE queue_id=?", (queue_id,)
        ).fetchone()[0]
    assert state == "attempting"


@pytest.mark.asyncio
async def test_gateway_releases_failed_restart_claim_and_continues_dispatching():
    failed_id = inbox.record_event(
        "failed-session", _event(text="first"), adapter_profile="default"
    )
    delivered_id = inbox.record_event(
        "delivered-session", _event(text="second"), adapter_profile="default"
    )
    _orphan(failed_id)
    _orphan(delivered_id)
    runner, adapter = make_restart_runner()
    adapter.handle_message = AsyncMock(side_effect=[RuntimeError("dispatch failed"), None])

    count = await GatewayRunner._drain_restart_inbox(runner)

    assert count == 1
    assert adapter.handle_message.await_count == 2
    reclaimed = inbox.claim_recoverable(
        deliverable_targets={("telegram", "default")}
    )
    assert [row["queue_id"] for row in reclaimed] == [failed_id]


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_first', [False, True])
async def test_profile_claim_failure_does_not_strand_healthy_live_owner(tmp_path, monkeypatch, failed_first):
    runner, adapter = make_restart_runner()
    home = inbox._db_path().parent
    other = tmp_path / 'other-profile'
    other.mkdir()
    primary_id = inbox.record_event('primary-session', _event(), adapter_profile='default')
    _orphan(primary_id)
    with monkeypatch.context() as scoped:
        scoped.setattr(inbox, '_db_path', lambda: other / 'state.db')
        event = _event(message_id='other-input')
        event.source.profile = 'other'
        secondary_id = inbox.record_event('secondary-session', event, adapter_profile='other')
        _orphan(secondary_id)
    runner.config.multiplex_profiles = True
    runner._profile_adapters = {'other': {Platform.TELEGRAM: adapter}}
    monkeypatch.setattr('gateway.run._multiplex_profile_homes', lambda config: [('other', other)])
    real_reconcile = runner._reconcile_restart_recovery

    def ordered_reconcile():
        assert real_reconcile()
        paths = [str((home / 'state.db').resolve()), str((other / 'state.db').resolve())]
        if failed_first:
            paths.reverse()
        runner._restart_inbox_blocked = {p: runner._restart_inbox_blocked[p] for p in paths}
        return True

    runner._reconcile_restart_recovery = ordered_reconcile
    real_claim = inbox.claim_recoverable
    failing = True

    def claim(**kwargs):
        if failing and str(kwargs['db_path']) == str((other / 'state.db').resolve()):
            raise sqlite3.OperationalError('profile database temporarily locked')
        return real_claim(**kwargs)

    monkeypatch.setattr(inbox, 'claim_recoverable', claim)
    adapter.handle_message = AsyncMock()
    assert await runner._drain_restart_inbox() == 1
    first = next(row for row in inbox.read_rows(home / 'state.db') if row['queue_id'] == primary_id)
    assert first['state'] == 'delivered'
    second = next(row for row in inbox.read_rows(other / 'state.db') if row['queue_id'] == secondary_id)
    assert second['state'] == 'pending'
    failing = False
    assert await runner._drain_restart_inbox() == 1
    assert await runner._drain_restart_inbox() == 0
    assert [c.args[0].message_id for c in adapter.handle_message.await_args_list] == ['msg-1', 'other-input']


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["entry", "reconcile", "claim", "dispatch"])
@pytest.mark.parametrize("flag,value", [("_draining", True), ("_running", False)])
async def test_shutdown_defers_undispatched_inbox_without_spending_attempts(monkeypatch, stage, flag, value):
    queued = []
    for index in range(2):
        queue_id = inbox.record_event(f"session-{index}", _event(message_id=f"msg-{index}"))
        _orphan(queue_id)
        queued.append(queue_id)
    original = {row["queue_id"]: row["event_json"] for row in inbox.read_rows(inbox._db_path())}
    runner, adapter = make_restart_runner()
    runner._schedule_restart_inbox_drain = lambda: None
    reconcile, claim = runner._reconcile_restart_recovery, inbox.claim_recoverable

    def stop():
        setattr(runner, flag, value)

    def reconcile_then_stop():
        result = reconcile()
        if stage == "reconcile":
            stop()
        return result

    def claim_then_stop(**kwargs):
        result = claim(**kwargs)
        if stage == "claim":
            stop()
        return result

    async def handle(event):
        if stage == "dispatch":
            stop()

    runner._reconcile_restart_recovery = reconcile_then_stop
    monkeypatch.setattr(inbox, "claim_recoverable", claim_then_stop)
    adapter.handle_message = AsyncMock(side_effect=handle)
    if stage == "entry":
        stop()
    count = await runner._drain_restart_inbox()
    expected = 1 if stage == "dispatch" else 0
    assert count == adapter.handle_message.await_count == expected
    rows = inbox.read_rows(inbox._db_path())
    pending = [row for row in rows if row["state"] == "pending"]
    assert len(pending) == 2 - expected
    assert all(row["attempts"] == 0 for row in pending)
    assert all(row["event_json"] == original[row["queue_id"]] for row in rows)
    assert all(row["state"] in ("pending", "delivered") for row in rows)
    # Restore admission: the exact pending rows remain claimable, once each.
    runner._running, runner._draining = True, False
    runner._reconcile_restart_recovery = reconcile
    monkeypatch.setattr(inbox, "claim_recoverable", claim)
    adapter.handle_message = AsyncMock()
    assert await runner._drain_restart_inbox() == 2 - expected
    rows = inbox.read_rows(inbox._db_path())
    assert {row["queue_id"] for row in rows} == set(queued)
    assert all(row["state"] == "delivered" and row["attempts"] == 1 for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["adapter_task", "admission"])
async def test_shutdown_after_adapter_handoff_does_not_settle_rejected_input(monkeypatch, stage):
    queue_id = inbox.record_event("session-key", _event())
    _orphan(queue_id)
    runner, adapter = make_restart_runner()
    runner._schedule_restart_inbox_drain = lambda: None
    adapter._event_session_key = lambda _event: "session-key"
    observed = []

    async def processing_hook(name, event, *args, **kwargs):
        if name == "on_processing_start":
            observed.append(event)
            if stage == "adapter_task":
                runner._draining = True

    async def admit(event):
        if stage == "admission":
            runner._draining = True
        return None

    monkeypatch.setattr(adapter, "_run_processing_hook", processing_hook)
    monkeypatch.setattr(runner, "_hm_admit_event", admit)
    adapter.set_message_handler(runner._handle_message)
    assert await runner._drain_restart_inbox() == 1
    task = adapter._session_tasks["session-key"]
    await asyncio.wait_for(task, 5)
    row = next(row for row in inbox.read_rows(inbox._db_path()) if row["queue_id"] == queue_id)
    assert row["state"] == "pending" and row["attempts"] == 0
    assert row["owner_pid"] is None and row["owner_started_at"] is None
    assert observed[0]._restart_input_admission_failed is True


@pytest.mark.asyncio
@pytest.mark.parametrize('shutdown', [False, True])
async def test_busy_ordinary_adapter_completion_wakes_pending_restart_input(shutdown):
    runner, adapter = make_restart_runner()
    adapter.gateway_runner = runner
    event = _event(text='ordinary turn')
    key = runner._session_key_for_source(event.source)
    started, release, replayed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handle(incoming):
        if incoming.text == 'ordinary turn':
            started.set()
            await release.wait()
        else:
            replayed.set()
        return None

    adapter._message_handler = handle
    # Real adapter ownership and cleanup, with only the agent boundary replaced.
    await adapter.handle_message(event)
    await asyncio.wait_for(started.wait(), 5)
    ordinary = adapter._session_tasks[key]
    queue_id = inbox.record_event(key, _event(message_id='after', text='pending restart input'))
    _orphan(queue_id)
    try:
        assert await runner._drain_restart_inbox() == 0
        assert await runner._drain_restart_inbox() == 0
        assert runner._restart_inbox_busy_tasks == {ordinary}
        row = next(r for r in inbox.read_rows(inbox._db_path()) if r['queue_id'] == queue_id)
        assert (row['state'], row['attempts']) == ('pending', 0)
        if shutdown:
            runner._draining = True
        release.set()
        await asyncio.wait_for(ordinary, 5)
        if shutdown:
            await asyncio.sleep(0)
            assert not replayed.is_set()
            row = next(r for r in inbox.read_rows(inbox._db_path()) if r['queue_id'] == queue_id)
            assert (row['state'], row['attempts']) == ('pending', 0)
        else:
            await asyncio.wait_for(replayed.wait(), 5)
    finally:
        release.set()
        await asyncio.gather(ordinary, return_exceptions=True)
        await adapter.cancel_background_tasks()
        for task in tuple(runner._background_tasks):
            await task


@pytest.mark.asyncio
async def test_running_turn_release_wakes_inbox_but_stale_generation_does_not():
    runner, adapter = make_restart_runner()
    event = _event()
    runner.session_store.get_or_create_session(event.source)
    key = runner._session_key_for_source(event.source)
    state = runner._session_state(key)
    state.turn.agent = object()
    state.persistent.run_generation = 2
    queue_id = inbox.record_event(key, event)
    _orphan(queue_id)
    replayed = asyncio.Event()

    async def handle(incoming):
        replayed.set()

    adapter.handle_message = handle
    assert await runner._drain_restart_inbox() == 0
    assert await runner._drain_restart_inbox() == 0
    assert len(runner._restart_inbox_busy_sessions) == 1
    assert runner._release_running_agent_state(key, run_generation=1) is False
    assert not replayed.is_set()
    assert runner._release_running_agent_state(key, run_generation=2) is True
    await asyncio.wait_for(replayed.wait(), 5)
    for task in tuple(runner._background_tasks):
        await task
    row = next(r for r in inbox.read_rows(inbox._db_path()) if r['queue_id'] == queue_id)
    assert (row['state'], row['attempts']) == ('delivered', 1)
    assert runner._restart_inbox_busy_sessions == {}
