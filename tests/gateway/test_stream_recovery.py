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
    (True, "reset-sid", "old", False), (True, "sid", "successor", True),
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
    assert recovery.mark_recovered_reply(result, consumer)["final_response"] == "answer to the new question"
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
    assert recovery.mark_recovered_reply(result, consumer)["final_response"] == "Carry on with the pending step?"
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

    real_journal = recovery._journal
    calls = 0

    def locked(op):
        nonlocal calls
        calls += 1
        if calls == 2:  # the re-own transaction, after the preview read
            raise RuntimeError("state.db is locked")
        return real_journal(op)

    monkeypatch.setattr(recovery, "_journal", locked)
    consumer = GatewayStreamConsumer(SimpleNamespace(), "chat")
    restore(make_runner(event=resume_event("old", active="turn-2")), consumer)
    assert consumer.message_id == "m1"
    monkeypatch.setattr(recovery, "_journal", real_journal)
    assert recovery.load_preview("key", "sid", "old") == dict(content="partial", message_id="m1")


def test_successor_resume_finds_predecessor_if_reown_crashed_after_marker_publish():
    """The successor active marker may survive while the preview row is still keyed by its predecessor."""
    record(content="partial", message_id="m1")
    consumer = GatewayStreamConsumer(SimpleNamespace(), "chat")
    # The next process sees resume_turn_id=successor, but the row was never re-keyed.
    restore(make_runner(token="successor", event=resume_event("successor", active="successor-2")), consumer)
    assert consumer.message_id == "m1"
    assert recovery.load_preview("key", "sid", "successor-2") == dict(content="partial", message_id="m1")
    assert recovery.load_preview("key", "sid", "old") is None


@pytest.mark.asyncio
async def test_fresh_final_crash_after_ack_keeps_replacement_intent_not_old_owner():
    """An accepted fresh replacement whose ACK journaling is interrupted must not re-adopt the old preview."""
    calls = 0

    async def journal(content, message_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            record(content=content, message_id=message_id)
        else:
            raise RuntimeError("crash after replacement ACK")

    async def send(**kwargs):
        return SendResult(success=True, message_id="fresh-id")

    async def delete_message(chat_id, message_id):
        return True

    adapter = SimpleNamespace(send=send, delete_message=delete_message)
    consumer = GatewayStreamConsumer(adapter, "chat", config=StreamConsumerConfig(cursor=""),
                                     on_preview=journal)
    consumer._message_id = "old-id"
    consumer._preview_message_ids = {"old-id"}
    consumer._last_sent_text = "partial"
    assert await consumer._try_fresh_final("complete answer")
    row = recovery.load_preview("key", "sid", "old")
    assert row["message_id"] == "old-id"
    assert row["content"].startswith(recovery._REPLACEMENT_PREVIEW_PREFIX)
    resumed = GatewayStreamConsumer(adapter, "chat")
    restore(make_runner(), resumed)
    assert resumed.message_id is None
    assert resumed._recovered_send_uncertain is True
    assert recovery.load_preview("key", "sid", "old") is None


@pytest.mark.asyncio
async def test_split_head_ack_is_journaled_before_split_send_returns():
    """An accepted sealed head is owned durably before the caller can advance the split state."""
    async def journal(content, message_id):
        record(content=content, message_id=message_id)

    async def send(**kwargs):
        return SendResult(success=True, message_id="head-id")

    consumer = GatewayStreamConsumer(SimpleNamespace(send=send), "chat", on_preview=journal)
    consumer._split_delivery_journal_pending = True
    assert await consumer._send_new_chunk("sealed head", None) == "head-id"
    row = recovery.load_preview("key", "sid", "old")
    assert row["content"].startswith(recovery._SPLIT_PREVIEW_PREFIX)
    assert "head-id" in row["content"]


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
    delivered = recovery.mark_recovered_reply(result, consumer)
    assert delivered["final_response"] == ledger.RECOVERED_MARKER + "real re-answer"
    assert delivered["recovered_reply"] is True
    # The agent's own result is never mutated: only the delivered copy carries the label.
    assert result == {"final_response": "real re-answer", "completed": True, "messages": messages}
    assert messages == [{"role": "assistant", "content": "real re-answer"}]
    for flags in ({"failed": True}, {"interrupted": True}, {"completed": False}):
        unfinished = {"final_response": "diagnostic", **flags}
        assert recovery.mark_recovered_reply(unfinished, consumer) is unfinished
        assert unfinished["final_response"] == "diagnostic"
    ordinary = {"final_response": "ordinary", "completed": True}
    assert recovery.mark_recovered_reply(ordinary, GatewayStreamConsumer(SimpleNamespace(), "chat")) is ordinary
    # Idempotent: an already-marked copy is not marked twice.
    assert recovery.mark_recovered_reply(delivered, consumer)["final_response"] == delivered["final_response"]


@pytest.mark.asyncio
async def test_adopted_preview_is_edited_even_when_the_re_answer_is_identical():
    # The row is write-ahead: a crash between the journal write and the edit leaves text
    # the user never saw. A byte-identical re-answer must still reach the message.
    record(content="complete answer", message_id="m1")
    edits = []

    async def edit_message(**kwargs):
        edits.append((kwargs["message_id"], kwargs["content"]))
        return SendResult(success=True, message_id=kwargs["message_id"])

    consumer = GatewayStreamConsumer(SimpleNamespace(edit_message=edit_message), "chat",
                                     config=StreamConsumerConfig(cursor=""))
    restore(make_runner(), consumer)
    assert consumer.message_id == "m1" and consumer._last_sent_text == "complete answer"
    assert await consumer._edit_existing("complete answer", finalize=False, is_turn_final=False)
    assert edits == [("m1", "complete answer")]
    # Only the first edit is forced; afterwards identical text is skipped as usual.
    assert await consumer._edit_existing("complete answer", finalize=False, is_turn_final=False)
    assert edits == [("m1", "complete answer")]


@pytest.mark.asyncio
async def test_failed_forced_edit_resends_the_whole_re_answer():
    # The adopted text is write-ahead and the forced edit never landed: nothing is known to be
    # on screen, so the fallback must send the complete re-answer, not suppress or trim it.
    record(content="complete answer", message_id="m1")
    sends = []

    async def edit_message(**kwargs):
        return SendResult(success=False, error="message not found")

    async def send(**kwargs):
        sends.append(kwargs["content"])
        return SendResult(success=True, message_id="m2")

    consumer = GatewayStreamConsumer(SimpleNamespace(edit_message=edit_message, send=send), "chat",
                                     config=StreamConsumerConfig(cursor=""))
    restore(make_runner(), consumer)
    assert not await consumer._edit_existing("complete answer", finalize=False, is_turn_final=False)
    assert consumer._fallback_final_send and consumer._visible_prefix() == ""
    assert not consumer.has_durably_delivered_text("complete answer")
    assert consumer.delivered_final_matches("complete answer") is not True
    await consumer._send_fallback_final("complete answer")
    assert sends == ["complete answer"]


@pytest.mark.asyncio
async def test_adopted_preview_survives_a_leading_segment_boundary():
    # A resumed turn that opens with a tool call or commentary must still finish in the
    # adopted message, and the unconfirmed partial is never recorded as a delivered segment.
    record(content="partial", message_id="m1")
    edits, sends = [], []

    async def edit_message(**kwargs):
        edits.append((kwargs["message_id"], kwargs["content"]))
        return SendResult(success=True, message_id=kwargs["message_id"])

    async def send(**kwargs):
        sends.append(kwargs["content"])
        return SendResult(success=True, message_id="new")

    consumer = GatewayStreamConsumer(SimpleNamespace(edit_message=edit_message, send=send), "chat",
                                     config=StreamConsumerConfig(cursor=""))
    restore(make_runner(), consumer)
    consumer._reset_segment_state(preserve_no_edit=True)
    assert consumer.message_id == "m1" and consumer._delivered_segment_texts == []
    assert not consumer.has_delivered_text("partial")
    assert await consumer._send_or_edit("partial and the rest", finalize=False, is_turn_final=False)
    assert edits == [("m1", "partial and the rest")] and sends == []
    # Once confirmed, the next boundary is ordinary: the segment counts and the id is released.
    consumer._reset_segment_state()
    assert consumer.message_id is None
    assert consumer._delivered_segment_texts == ["partial and the rest"]


@pytest.mark.asyncio
async def test_post_ack_journal_write_skipped_when_message_id_is_unchanged():
    writes = []

    async def journal(content, message_id):
        writes.append((content, message_id))

    async def send(**kwargs):
        return SendResult(success=True, message_id=None)  # ACK carries no platform id

    consumer = GatewayStreamConsumer(SimpleNamespace(send=send), "chat",
                                     config=StreamConsumerConfig(cursor=""), on_preview=journal)
    assert await consumer._first_send("answer", finalize=False)
    assert writes == [("answer", None)]


@pytest.mark.asyncio
async def test_recovered_marker_never_reaches_the_fallback_transcript(tmp_path):
    from gateway.config import GatewayConfig
    from gateway.run_turn import GatewayTurnMixin
    from gateway.session import AsyncSessionStore, SessionStore
    from hermes_state import SessionDB
    from unittest.mock import AsyncMock

    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    store._db = SessionDB(db_path=tmp_path / "state.db")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    entry = store.get_or_create_session(source)
    store._db.create_session(session_id=entry.session_id, source="telegram", user_id="u1")
    history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    store._db.replace_messages(entry.session_id, history)
    try:
        runner = object.__new__(GatewayTurnMixin)
        runner.async_session_store = AsyncSessionStore(store)
        runner._session_db = None
        runner._refresh_agent_cache_message_count = AsyncMock()
        consumer = GatewayStreamConsumer(SimpleNamespace(), "chat")
        consumer._recovered_send_uncertain = True
        agent_result = {"final_response": "real re-answer", "completed": True,
                        "messages": history, "history_offset": len(history)}
        delivered = recovery.mark_recovered_reply(agent_result, consumer)
        assert agent_result["final_response"] == "real re-answer"
        prepared = SimpleNamespace(history=history, persist_user_message=None, message_text="continue",
                                   persist_user_timestamp=None, persist_user_display_kind=None,
                                   persistence_owner=None)
        # No new agent messages: persistence falls back to the delivered response text.
        await runner._hmwa_persist_turn_transcript(
            event=SimpleNamespace(message_id="msg-1", internal=False), source=source, session_entry=entry,
            session_key=entry.session_key, agent_result=delivered, agent_messages=history, prepared=prepared,
            response=delivered["final_response"], agent_failed_early=False,
            hidden_reasoning_incomplete=False, is_context_overflow_failure=False,
        )
        transcript = await runner.async_session_store.load_transcript(entry.session_id)
        assert transcript[-1]["role"] == "assistant"
        assert transcript[-1]["content"] == "real re-answer"
        assert all(ledger.RECOVERED_MARKER not in str(row.get("content")) for row in transcript)
    finally:
        store._db.close()


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
    assert events[0][0] == "journal"
    assert events[0][1].startswith(recovery._REPLACEMENT_PREVIEW_PREFIX)
    assert events[0][2] == "m1"
    assert events[1:] == [("journal", "complete answer", "fresh-id"), ("delete", "m1")]
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
async def test_crash_after_split_never_adopts_a_sealed_head(prestreamed, monkeypatch):
    consumer = GatewayStreamConsumer(_split_adapter(), "chat", StreamConsumerConfig(
        edit_interval=0.01, buffer_threshold=5, cursor=""), on_preview=_journal_to_db)
    # SIGKILL runs no Python: run() swallows CancelledError into _on_cancelled(), whose
    # best-effort finalize a real kill never reaches. Stub it so cancel only stops the loop.
    killed = []

    async def sigkill():
        killed.append(True)

    monkeypatch.setattr(consumer, "_on_cancelled", sigkill)
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
    # The crash point: what the next process reads is whatever is durable right now.
    at_crash = recovery.load_preview("key", "sid", "old")
    task.cancel()
    await asyncio.wait_for(task, timeout=10)  # run() swallows the cancel and returns
    assert killed == [True] and consumer._final_response_sent is False  # no finalize ran
    assert at_crash is None
    assert recovery.load_preview("key", "sid", "old") is None
    resumed = GatewayStreamConsumer(SimpleNamespace(), "chat")
    restore(make_runner(), resumed)
    assert resumed.message_id is None and resumed._already_sent is False
