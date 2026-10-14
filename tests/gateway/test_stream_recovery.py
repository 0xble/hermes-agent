"""Durable real-preview ownership and fencing at crash/resume boundaries."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from gateway import delivery_ledger as ledger
from gateway import stream_recovery as recovery
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run_startup import GatewayStartupMixin
from gateway.session import SessionSource
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


@pytest.fixture(autouse=True)
def isolated_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "_db_path", lambda: tmp_path / "state.db")


def record(**changes):
    values = dict(turn_id="old", session_key="key", session_id="sid", content="visible", message_id="m1")
    values.update(changes)
    recovery.record_preview(**values)


def test_snapshot_is_durable_and_fenced():
    record()
    assert recovery.load_preview("key", "sid", "old") == dict(content="visible", message_id="m1")
    assert recovery.load_preview("other", "sid", "old") is None
    assert recovery.load_preview("key", "other", "old") is None
    assert recovery.load_preview("key", "sid", "successor") is None
    record(content="complete intended edit")
    assert recovery.load_preview("key", "sid", "old")["content"] == "complete intended edit"
    recovery.discard_preview("old")
    assert recovery.load_preview("key", "sid", "old") is None


@pytest.mark.asyncio
async def test_first_send_records_before_transport_and_id_after_ack():
    gate = asyncio.Event()
    called = asyncio.Event()

    async def journal(content, message_id):
        record(content=content, message_id=message_id)

    async def send(**kwargs):
        assert recovery.load_preview("key", "sid", "old") == dict(content="answer", message_id=None)
        called.set()
        await gate.wait()
        return SendResult(success=True, message_id="accepted-id")

    adapter = SimpleNamespace(send=send)
    consumer = GatewayStreamConsumer(adapter, "chat", config=StreamConsumerConfig(cursor=" ▉"), on_preview=journal)
    task = asyncio.create_task(consumer._first_send("answer ▉", finalize=False))
    await called.wait()
    assert recovery.load_preview("key", "sid", "old")["message_id"] is None
    gate.set()
    assert await task
    assert recovery.load_preview("key", "sid", "old") == dict(content="answer", message_id="accepted-id")


@pytest.mark.asyncio
async def test_edit_write_ahead_retains_known_platform_id():
    async def journal(content, message_id):
        record(content=content, message_id=message_id)

    async def edit(**kwargs):
        assert recovery.load_preview("key", "sid", "old") == dict(content="complete answer", message_id="m1")
        return SendResult(success=True, message_id="m1")

    adapter = SimpleNamespace(edit_message=edit)
    consumer = GatewayStreamConsumer(adapter, "chat", config=StreamConsumerConfig(cursor=""), on_preview=journal)
    consumer._message_id = "m1"
    consumer._last_sent_text = "partial"
    assert await consumer._edit_existing("complete answer", finalize=False, is_turn_final=False)


def test_schedule_resume_event_identity_restores_preview():
    record()
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="chat", chat_type="private", user_id="user", message_id="inbound",
    )
    event = GatewayStartupMixin._build_startup_resume_event(source, "old")
    assert event.internal is True
    assert getattr(event, "_outbox_admission_id") == "resume:old"
    consumer = GatewayStreamConsumer(SimpleNamespace(), "chat")
    restore(make_runner(event=event), consumer)
    assert consumer.message_id == "m1"
    assert consumer._already_sent is True


def resume_event(token="old", *, active="successor-token", internal=True, admission=None):
    event = SimpleNamespace(text="", internal=internal, _gateway_active_turn_token=active)
    event._outbox_admission_id = admission if admission is not None else f"resume:{token}"
    return event


def make_runner(*, pending=True, token="old", session_id="sid", event=None):
    entry = SimpleNamespace(resume_pending=pending, resume_turn_id=token, session_id=session_id)
    store = SimpleNamespace(_lock=threading.RLock(), _ensure_loaded_locked=lambda: None, _entries={"key": entry})
    state = SimpleNamespace(turn=SimpleNamespace(event=event if event is not None else resume_event(token)))
    return SimpleNamespace(session_store=store, _peek_session_state=lambda key: state if key == "key" else None)


def restore(runner, consumer, policy="continue"):
    recovery.restore_preview(runner, SimpleNamespace(session_key="key", session_id="sid"), consumer, policy=policy)


@pytest.mark.parametrize("pending,session_id,token,restored", [
    (True, "sid", "old", True), (False, "sid", "old", False),
    (True, "reset-sid", "old", False), (True, "sid", "successor", False),
])
def test_resume_reuses_only_exact_predecessor(pending, session_id, token, restored):
    record()
    consumer = GatewayStreamConsumer(SimpleNamespace(), "chat")
    restore(make_runner(pending=pending, token=token, session_id=session_id), consumer)
    assert consumer.message_id == ("m1" if restored else None)
    assert consumer._already_sent is restored
    assert consumer._recovered_send_uncertain is False
    assert consumer.cfg.buffer_only is False


@pytest.mark.parametrize("event", [
    # A real user message admitted while resume_pending lingers (breaker trip, stale
    # marker, adapter offline): it carries its own transport identity, not the resume's.
    SimpleNamespace(text="new question", internal=False, _gateway_active_turn_token="user-turn",
                    _outbox_admission_id=None, message_id="in-2"),
    # Any other internal turn (e.g. a delegation notice) is not the resume either.
    resume_event(admission="update:9"),
    resume_event(internal=False),
], ids=["user-inbound", "other-internal", "non-internal-spoof"])
@pytest.mark.parametrize("message_id", ["m1", None], ids=["acked", "ack-lost"])
def test_only_the_synthetic_resume_turn_adopts(event, message_id):
    record(message_id=message_id)
    consumer = GatewayStreamConsumer(SimpleNamespace(), "chat")
    restore(make_runner(event=event), consumer)
    assert consumer.message_id is None
    assert consumer._already_sent is False
    assert consumer._recovered_send_uncertain is False
    assert consumer.cfg.buffer_only is False
    result = {"final_response": "answer to the new question", "completed": True}
    recovery.mark_recovered_reply(result, consumer)
    assert result["final_response"] == "answer to the new question"
    # The predecessor row is untouched and still owned by the pending resume.
    assert recovery.load_preview("key", "sid", "old")["message_id"] == message_id


@pytest.mark.parametrize("policy", ["ask", None, "other"])
@pytest.mark.parametrize("message_id", ["m1", None], ids=["acked", "ack-lost"])
def test_ask_policy_leaves_partial_preview_untouched(policy, message_id):
    record(message_id=message_id)
    consumer = GatewayStreamConsumer(SimpleNamespace(), "chat")
    restore(make_runner(), consumer, policy=policy)
    assert consumer.message_id is None and consumer._already_sent is False
    assert consumer._recovered_send_uncertain is False and consumer.cfg.buffer_only is False
    result = {"final_response": "Carry on with the pending step?", "completed": True}
    recovery.mark_recovered_reply(result, consumer)
    assert result["final_response"] == "Carry on with the pending step?"
    assert recovery.load_preview("key", "sid", "successor-token") is None


def test_double_crash_reowns_preview_under_resumed_token():
    record(content="partial", message_id="m1")
    first = GatewayStreamConsumer(SimpleNamespace(), "chat")
    restore(make_runner(event=resume_event("old", active="turn-2")), first)
    assert first.message_id == "m1"
    # Crash #2 before the resumed turn writes anything: promotion makes turn-2 the
    # new resume_turn_id, and the next synthetic resume must still find the message.
    second = GatewayStreamConsumer(SimpleNamespace(), "chat")
    restore(make_runner(token="turn-2", event=resume_event("turn-2", active="turn-3")), second)
    assert second.message_id == "m1"
    assert second._last_sent_text == "partial"
    assert recovery.load_preview("key", "sid", "turn-3") == dict(content="partial", message_id="m1")
    # Each re-own moves the row: predecessors are discarded once the successor holds it.
    assert recovery.load_preview("key", "sid", "old") is None
    assert recovery.load_preview("key", "sid", "turn-2") is None


def test_failed_reown_keeps_the_predecessor_row(monkeypatch):
    record(content="partial", message_id="m1")

    def locked(**kwargs):
        raise RuntimeError("state.db is locked")

    monkeypatch.setattr(recovery, "record_preview", locked)
    consumer = GatewayStreamConsumer(SimpleNamespace(), "chat")
    restore(make_runner(event=resume_event("old", active="turn-2")), consumer)
    assert consumer.message_id == "m1"
    assert recovery.load_preview("key", "sid", "old") == dict(content="partial", message_id="m1")


def test_schema_and_purge_run_once_per_process(monkeypatch):
    calls = []
    real = recovery._prepare_journal
    monkeypatch.setattr(recovery, "_prepare_journal", lambda conn: (calls.append(1), real(conn)))
    for i in range(5):
        record(content=f"edit {i}")
    assert recovery.load_preview("key", "sid", "old")["content"] == "edit 4"
    recovery.discard_preview("old")
    assert len(calls) == 1


def test_stale_rows_are_purged_when_the_journal_is_prepared(tmp_path):
    record(turn_id="stale")
    recovery._journal(lambda conn: conn.execute(
        "UPDATE streamed_previews SET updated_at = 0 WHERE turn_id = 'stale'"))
    recovery._prepared_paths.discard(str(ledger._db_path()))  # a fresh process
    record(turn_id="fresh")
    assert recovery.load_preview("key", "sid", "stale") is None
    assert recovery.load_preview("key", "sid", "fresh") is not None


def test_journal_reprepares_a_table_dropped_underneath():
    record()
    recovery._journal(lambda conn: conn.execute("DROP TABLE streamed_previews"))
    record(content="after drop")
    assert recovery.load_preview("key", "sid", "old")["content"] == "after drop"


@pytest.mark.asyncio
async def test_journal_failure_warning_is_rate_limited(monkeypatch, caplog):
    import gateway.stream_consumer_transport as transport

    monkeypatch.setattr(transport, "_last_journal_warning", float("-inf"))

    async def journal(content, message_id):
        raise RuntimeError("state.db is locked")

    consumer = GatewayStreamConsumer(SimpleNamespace(), "chat", config=StreamConsumerConfig(cursor=""),
                                     on_preview=journal)
    with caplog.at_level("DEBUG", logger="gateway.stream_consumer"):
        for _ in range(5):
            await consumer._journal_preview("text", "m1")
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "journal write failed" in r.message]
    assert len(warnings) == 1


def test_ack_lost_first_send_does_not_invent_platform_id():
    record(message_id=None)
    consumer = GatewayStreamConsumer(SimpleNamespace(), "chat")
    restore(make_runner(), consumer)
    assert consumer.message_id is None
    assert consumer._already_sent is False
    assert consumer._recovered_send_uncertain is True
    assert consumer.cfg.buffer_only is True
    messages = [{"role": "assistant", "content": "real re-answer"}]
    result = {"final_response": "real re-answer", "completed": True, "messages": messages}
    recovery.mark_recovered_reply(result, consumer)
    assert result["final_response"] == ledger.RECOVERED_MARKER + "real re-answer"
    assert messages == [{"role": "assistant", "content": "real re-answer"}]
    for flags in ({"failed": True}, {"interrupted": True}, {"completed": False}):
        unfinished = {"final_response": "diagnostic", **flags}
        recovery.mark_recovered_reply(unfinished, consumer)
        assert unfinished["final_response"] == "diagnostic"
    ordinary = {"final_response": "ordinary", "completed": True}
    recovery.mark_recovered_reply(ordinary, GatewayStreamConsumer(SimpleNamespace(), "chat"))
    assert ordinary["final_response"] == "ordinary"
    recovery.mark_recovered_reply(result, consumer)
    assert result["final_response"] == ledger.RECOVERED_MARKER + "real re-answer"


@pytest.mark.asyncio
async def test_journal_failure_after_ack_never_causes_a_second_send():
    sends = []

    async def journal(content, message_id):
        if message_id:
            raise RuntimeError("state.db is locked")

    async def send(**kwargs):
        sends.append(kwargs["content"])
        return SendResult(success=True, message_id="accepted-id")

    consumer = GatewayStreamConsumer(SimpleNamespace(send=send), "chat",
                                     config=StreamConsumerConfig(cursor=""), on_preview=journal)
    assert await consumer._first_send("answer", finalize=False)
    assert sends == ["answer"]
    assert consumer.message_id == "accepted-id" and consumer._already_sent is True


@pytest.mark.asyncio
async def test_fresh_final_re_records_replacement_before_deleting_preview():
    events = []

    async def journal(content, message_id):
        events.append(("journal", content, message_id))
        record(content=content, message_id=message_id)

    async def send(**kwargs):
        return SendResult(success=True, message_id="fresh-id")

    async def delete_message(chat_id, message_id):
        events.append(("delete", message_id))
        return True

    consumer = GatewayStreamConsumer(SimpleNamespace(send=send, delete_message=delete_message), "chat",
                                     config=StreamConsumerConfig(cursor=""), on_preview=journal)
    consumer._message_id = "m1"
    consumer._last_sent_text = "partial"
    assert await consumer._try_fresh_final("complete answer")
    assert events == [("journal", "complete answer", "fresh-id"), ("delete", "m1")]
    assert recovery.load_preview("key", "sid", "old") == dict(content="complete answer", message_id="fresh-id")


@pytest.mark.asyncio
async def test_silence_marker_forgets_the_deleted_preview():
    record()

    async def journal(content, message_id):
        if not content:
            recovery.discard_preview("old")

    async def delete_message(chat_id, message_id):
        assert recovery.load_preview("key", "sid", "old") is None
        return True

    consumer = GatewayStreamConsumer(SimpleNamespace(delete_message=delete_message), "chat", on_preview=journal)
    consumer._message_id = "m1"
    await consumer._suppress_silence_marker()
    assert recovery.load_preview("key", "sid", "old") is None


def _split_adapter():
    from tests.gateway.test_stream_consumer_oversized_leftover import _make_plain_adapter, _wire

    adapter = _make_plain_adapter()
    _wire(adapter)
    return adapter


async def _journal_to_db(content, message_id):
    if not content:
        recovery.discard_preview("old")
    else:
        record(content=content, message_id=message_id)


HEAD = "head line\n" * 60
TAIL = "".join(f"tail line {i:03d} filler filler filler filler filler\n" for i in range(240))


@pytest.mark.asyncio
async def test_split_delivery_leaves_no_preview_row():
    consumer = GatewayStreamConsumer(_split_adapter(), "chat", StreamConsumerConfig(
        edit_interval=0.01, buffer_threshold=5, cursor=""), on_preview=_journal_to_db)
    consumer.on_delta(HEAD)
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.06)
    assert recovery.load_preview("key", "sid", "old") is not None  # the head is journaled
    consumer.on_delta(TAIL)
    await asyncio.sleep(0.12)
    consumer.finish()
    await asyncio.wait_for(task, timeout=10)
    assert consumer._turn_split_delivery is True
    assert recovery.load_preview("key", "sid", "old") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("prestreamed", [True, False], ids=["seal-existing", "split-first-send"])
async def test_crash_after_split_never_adopts_a_sealed_head(prestreamed):
    consumer = GatewayStreamConsumer(_split_adapter(), "chat", StreamConsumerConfig(
        edit_interval=0.01, buffer_threshold=5, cursor=""), on_preview=_journal_to_db)
    consumer.on_delta(HEAD if prestreamed else HEAD + TAIL)
    task = asyncio.create_task(consumer.run())
    if prestreamed:
        await asyncio.sleep(0.06)
        consumer.on_delta(TAIL)
    for _ in range(200):
        if consumer._turn_split_delivery:
            break
        await asyncio.sleep(0.01)
    assert consumer._turn_split_delivery is True
    await asyncio.sleep(0.05)  # the tail preview goes out after the split
    # kill -9 here: no finalize ever runs; the next process sees only the journal.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recovery.load_preview("key", "sid", "old") is None
    resumed = GatewayStreamConsumer(SimpleNamespace(), "chat")
    restore(make_runner(), resumed)
    assert resumed.message_id is None and resumed._already_sent is False
