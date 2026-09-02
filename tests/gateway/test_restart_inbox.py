"""Crash-durable inbound queue for messages accepted during restart drain."""

import sqlite3
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway import restart_inbox as inbox
from gateway.run import GatewayRunner
from tests.gateway.restart_test_helpers import make_restart_runner


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
