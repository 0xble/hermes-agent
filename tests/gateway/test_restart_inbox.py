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
