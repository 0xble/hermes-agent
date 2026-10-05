"""S4.1 durable admission and outbound delivery boundary."""

import asyncio
import contextlib
import errno
import multiprocessing as mp
import os
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gateway.outbox import Outbox, active_turn, bind_turn, clear_turn, recover, _uncertain, _store_io
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.run_inbound import GatewayInboundMixin
from gateway.config import GatewayConfig, Platform, PlatformConfig, load_gateway_config
from unittest.mock import AsyncMock
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
async def test_approval_and_clarify_cards_are_receipted_once(tmp_path):
    adapter = _fake_adapter(True, [])
    adapter._reply_to_message_id_for_send = lambda *a, **kw: None
    adapter._link_preview_kwargs = lambda: {}
    adapter._thread_kwargs_for_send = lambda *a, **kw: {}
    sent = []

    async def send_raw(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(message_id=len(sent))

    adapter._send_message_with_thread_fallback = send_raw
    bind_turn(tmp_path, "approval-turn")
    try:
        for text, code in (("Approve?", "ea:once:1"), ("Clarify?", "cl:2:0")):
            card = SimpleNamespace(to_dict=lambda: {"inline_keyboard": [[{"text": text, "callback_data": code}]]})
            result = await adapter._send_control_message(
                "chat", text, parse_mode="HTML", thread_id=None, metadata=None,
                reply_markup=card)
            assert result.message_id == len(sent)
        assert len(sent) == 2
        assert Outbox(tmp_path).pending() == []
        assert await recover(Outbox(tmp_path), adapter) == (0, 0)
        assert len(sent) == 2
    finally:
        clear_turn()


@pytest.mark.asyncio
async def test_attachment_is_snapshotted_before_dispatch_and_not_resent(tmp_path):
    original = tmp_path / "scratch.txt"
    original.write_bytes(b"durable attachment")
    adapter = _fake_adapter(True, [])
    dispatched = []

    async def send_file(label, path, *args):
        dispatched.append((path, open(path, "rb").read()))
        return SendResult(success=True, message_id="file-1")

    adapter._send_local_file = send_file
    bind_turn(tmp_path, "attachment-turn")
    try:
        result = await adapter.send_document("chat", str(original))
        assert result.success
        original.unlink()
        assert dispatched[0][0] != str(original)
        assert dispatched[0][1] == b"durable attachment"
        assert not Path(dispatched[0][0]).exists()
        assert Outbox(tmp_path).pending() == []
        assert await recover(Outbox(tmp_path), adapter) == (0, 0)
        assert len(dispatched) == 1
    finally:
        clear_turn()


def test_flag_parses_from_real_profile_config_and_defaults_off(tmp_path, monkeypatch):
    homes = [tmp_path / "a", tmp_path / "b", tmp_path / "a"]
    for home in {homes[0], homes[1]}:
        home.mkdir()
    (homes[0] / "config.yaml").write_text("gateway:\n  durable_outbox:\n    enabled: true\n")
    (homes[1] / "config.yaml").write_text("gateway:\n  durable_outbox:\n    enabled: false\n")
    assert GatewayConfig.from_dict({}).durable_outbox_enabled is False
    assert GatewayConfig.from_dict({"gateway": {"durable_outbox": {"enabled": True}}}).to_dict()["durable_outbox"]["enabled"]
    with pytest.raises(ValueError, match="boolean"):
        GatewayConfig.from_dict({"gateway": {"durable_outbox": {"enabled": "false"}}})
    for home, expected in zip(homes, (True, False, True)):
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert load_gateway_config().durable_outbox_enabled is expected


def _die_before_dispatch(home):
    os.environ["HERMES_HOME"] = str(home)
    from gateway.run import GatewayRunner

    class CrashingRunner(GatewayRunner):
        async def _handle_admitted_message(self, event):
            Outbox(home).enqueue(event._outbox_turn_id, "send", {"chat_id": "chat", "content": "reply"})
            os._exit(0)

    runner = CrashingRunner.__new__(CrashingRunner)
    asyncio.run(runner._handle_message(SimpleNamespace(_outbox_turn_id="turn", _outbox_home=home)))



def _die_after_begin(home):
    store = Outbox(home)
    row = store.enqueue("turn", "send", {"chat_id": "chat", "content": "reply"})
    store.begin_send(row)
    os._exit(0)


def test_real_process_recovery_before_dispatch_and_ambiguous_after_begin(tmp_path):
    child = mp.Process(target=_die_before_dispatch, args=(tmp_path,))
    child.start()
    child.join(timeout=10)
    assert child.exitcode == 0
    store = Outbox(tmp_path)
    pending = store.pending()
    assert len(pending) == 1
    sends = []
    assert asyncio.run(recover(store, _fake_adapter(True, sends))) == (1, 0)
    assert sends == [("chat", "reply")]
    assert store.pending() == []
    child = mp.Process(target=_die_after_begin, args=(tmp_path / "other",))
    child.start()
    child.join(timeout=10)
    assert child.exitcode == 0
    recovered = Outbox(tmp_path / "other")
    assert recovered.pending() == []
    assert len(recovered.ambiguous()) == 1


def test_fifo_receipts_and_edit_order(tmp_path):
    store = Outbox(tmp_path)
    first = store.enqueue("turn", "send", {"content": "draft"})
    second = store.enqueue("turn", "edit_message", {"content": "final", "message_id": "msg-1"})
    third = store.enqueue("turn", "attachment", {"file": "a"})
    assert [r.sequence for r in (first, second, third)] == [1, 2, 3]
    assert store.begin_send(second)
    assert store.begin_send(first)
    assert store.pending() == [third]
    store.receipt(first, message_id="msg-1", success=True)
    store.receipt(second, message_id="msg-1", success=True)
    assert store.pending() == [third]
    assert store.begin_send(third)
    store.receipt(third, message_id="msg-2", success=True)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT edit_status FROM outbox WHERE sequence=2").fetchone()[0] == "success"


def test_admission_scoped_by_profile_and_original_result(tmp_path):
    a = Outbox(tmp_path / "a")
    b = Outbox(tmp_path / "b")
    turn, fresh = a.admit("a", "telegram", "transport-1", "message")
    assert fresh
    a.finish_admission(turn, "answer")
    assert a.admit("a", "telegram", "transport-1", "message") == (turn, False)
    assert a.original_result(turn) == "answer"
    other, fresh = b.admit("b", "telegram", "transport-1", "message")
    assert fresh and other != turn
    assert a.admit("a", "telegram", "transport-1", "message") == (turn, False)
    synthetic, _ = a.admit("a", "telegram", None, "synthetic", fallback_id="stable-id")
    assert a.admit("a", "telegram", None, "synthetic", fallback_id="stable-id") == (synthetic, False)


def _fake_adapter(enabled, sends):
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.gateway_runner = SimpleNamespace(config=SimpleNamespace(durable_outbox_enabled=enabled))
    adapter._bot = object()
    adapter._chat_send_lock = lambda chat: contextlib.nullcontext()

    async def send_locked(chat_id, content, reply_to, metadata):
        sends.append((chat_id, content))
        return SendResult(success=True, message_id=f"m{len(sends)}")

    adapter._send_text_locked = send_locked
    return adapter


@pytest.mark.asyncio
async def test_telegram_egress_and_boot_recovery_are_flagged(tmp_path):
    sends = []
    adapter = _fake_adapter(True, sends)
    bind_turn(tmp_path, "turn-1")
    try:
        result = await adapter.send("chat", "first")
        assert result.success and result.message_id == "m1"
        store = Outbox(tmp_path)
        assert store.pending() == []
        assert store.ambiguous() == []
        queued = store.enqueue("turn-1", "send", {"chat_id": "chat", "content": "second"})
        assert queued.sequence == 2
        assert await recover(Outbox(tmp_path), adapter) == (1, 0)
        assert sends == [("chat", "first"), ("chat", "second")]
    finally:
        clear_turn()

    off = _fake_adapter(False, sends)
    bind_turn(tmp_path / "off", "turn-2")
    try:
        await off.send("chat", "unchanged")
        assert not (tmp_path / "off" / "gateway-outbox.db").exists()
    finally:
        clear_turn()


@pytest.mark.asyncio
async def test_ambiguous_telegram_send_held_without_duplicate(tmp_path):
    sends = []
    adapter = _fake_adapter(True, sends)

    async def uncertain(chat_id, content, reply_to, metadata):
        sends.append((chat_id, content))
        raise RuntimeError("transport accepted but receipt lost")

    adapter._send_text_locked = uncertain
    bind_turn(tmp_path, "turn-1")
    try:
        with pytest.raises(RuntimeError, match="receipt lost"):
            await adapter.send("chat", "final")
        assert len(Outbox(tmp_path).ambiguous()) == 1
        healthy = _fake_adapter(True, sends)
        assert await recover(Outbox(tmp_path), healthy) == (0, 1)
        assert sends == [("chat", "final")]
    finally:
        clear_turn()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    SendResult(False, error="Bad Request: chat not found"),
    SendResult(False, error="message_too_long", error_kind="too_long"),
    SendResult(False, error="flood_control:120", retry_after=120),
    SendResult(False, error="Not connected"),
    SendResult(False, error="draft_rejected"),
    SendResult(False, error="network timeout", retryable=True),
])
async def test_failed_send_does_not_hold_different_payload(tmp_path, failure):
    sends = []
    adapter = _fake_adapter(True, sends)
    async def first_fails(chat_id, content, reply_to, metadata):
        sends.append(content)
        return failure if len(sends) == 1 else SendResult(True, message_id="fallback")
    adapter._send_text_locked = first_fails
    bind_turn(tmp_path, "fallback-turn")
    try:
        first = await adapter.send("chat", "formatted")
        assert first.success is (failure.retry_after is not None)
        assert first.deferred is (failure.retry_after is not None)
        assert (await adapter.send("chat", "plain fallback")).success
        assert sends == ["formatted", "plain fallback"]
        states = [row.state for row in Outbox(tmp_path).all_rows()]
        expected = ("ambiguous" if failure.error == "network timeout" else
                    "pending" if failure.retry_after is not None else "failed_unsent")
        assert states[0] == expected
    finally:
        clear_turn()


@pytest.mark.asyncio
async def test_not_connected_edit_does_not_hold_final_send(tmp_path):
    sends = []
    adapter = _fake_adapter(True, sends)
    adapter._bot = None
    bind_turn(tmp_path, "edit-fallback")
    try:
        assert not (await adapter.edit_message("chat", "old", "updated", finalize=True)).success
        adapter._bot = object()
        assert (await adapter.send("chat", "updated")).success
        assert sends == [("chat", "updated")]
        assert [r.state for r in Outbox(tmp_path).all_rows()] == ["failed_unsent", "delivered"]
    finally:
        clear_turn()


@pytest.mark.asyncio
async def test_background_task_does_not_inherit_turn_egress(tmp_path):
    sends = []
    adapter = _fake_adapter(True, sends)
    bind_turn(tmp_path, "outer")
    try:
        row = Outbox(tmp_path).enqueue("outer", "send", {"chat_id": "chat", "content": "held"})
        assert Outbox(tmp_path).begin_send(row)
        async def background():
            assert active_turn() is None
            return await adapter.send("chat", "background")
        assert (await asyncio.create_task(background())).success
        assert sends == [("chat", "background")]
    finally:
        clear_turn()


@pytest.mark.asyncio
async def test_reentrant_inbound_preserves_outer_turn(tmp_path):
    from gateway.run_inbound import GatewayInboundMixin
    class Runner(GatewayInboundMixin):
        async def _handle_admitted_message(self, event):
            assert active_turn() is None
            return None
    bind_turn(tmp_path, "outer")
    try:
        assert await Runner()._handle_message(cast(Any, SimpleNamespace())) is None
        assert active_turn() == (tmp_path, "outer")
    finally:
        clear_turn()


@pytest.mark.asyncio
async def test_turn_owned_stream_child_binds_explicitly(tmp_path):
    from gateway.outbox import run_turn_child
    sends = []
    adapter = _fake_adapter(True, sends)
    bind_turn(tmp_path, "stream")
    try:
        async def stream_send():
            assert active_turn() == (tmp_path, "stream")
            return await adapter.send("chat", "stream-final")
        result = await asyncio.create_task(run_turn_child(stream_send(), active_turn()))
        assert result.success
        assert Outbox(tmp_path).all_rows()[0].state == "delivered"
    finally:
        clear_turn()


@pytest.mark.asyncio
async def test_flood_wait_retries_once_without_a_second_final(tmp_path):
    sends = []
    adapter = _fake_adapter(True, sends)
    async def limited(chat_id, content, reply_to, metadata):
        sends.append(content)
        if len(sends) == 1:
            return SendResult(False, error="flood_control:0.01", retry_after=0.01)
        return SendResult(True, message_id="one-final")
    adapter._send_text_locked = limited
    bind_turn(tmp_path, "flood-turn")
    try:
        result = await adapter.send("chat", "final")
        assert result.success and result.deferred
        await asyncio.sleep(0.1)
        assert sends == ["final", "final"]
        assert [r.state for r in Outbox(tmp_path).all_rows()] == ["delivered"]
        assert await recover(Outbox(tmp_path), adapter) == (0, 0)
        assert len(sends) == 2
    finally:
        clear_turn()


@pytest.mark.asyncio
async def test_final_retry_does_not_compete_with_inline_retry(tmp_path):
    sends = []
    adapter = _fake_adapter(True, sends)
    second_send = asyncio.Event()
    async def limited(chat_id, content, reply_to, metadata):
        sends.append(content)
        if len(sends) == 1:
            return SendResult(False, error="flood_control:0.01", retry_after=0.01)
        second_send.set()
        return SendResult(True, message_id="one-final")
    adapter._send_text_locked = limited
    bind_turn(tmp_path, "single-final")
    try:
        result = await adapter._send_with_retry("chat", "answer", max_retries=2, base_delay=0)
        assert result.success and result.deferred
        assert [r.state for r in Outbox(tmp_path).all_rows()] == ["pending"]
        await asyncio.wait_for(second_send.wait(), timeout=10)
        assert sends == ["answer", "answer"]
        rows = Outbox(tmp_path).all_rows()
        for _ in range(60):
            if rows[0].state == "delivered":
                break
            await asyncio.sleep(0.05)
            rows = Outbox(tmp_path).all_rows()
        assert len(rows) == 1 and rows[0].state == "delivered"
        assert rows[0].turn_id == "single-final"
    finally:
        clear_turn()


@pytest.mark.asyncio
async def test_non_json_metadata_is_defensively_stored(tmp_path):
    sends = []
    adapter = _fake_adapter(True, sends)
    bind_turn(tmp_path, "metadata")
    try:
        assert (await adapter.send("chat", "reply", metadata={"opaque": object()})).success
        assert sends == [("chat", "reply")]
        assert Outbox(tmp_path).all_rows()[0].state == "delivered"
    finally:
        clear_turn()


def test_ambiguous_expires_without_replay_and_is_visible(tmp_path):
    store = Outbox(tmp_path)
    row = store.enqueue("turn", "send", {"content": "uncertain"})
    assert store.begin_send(row)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET created_at=created_at-90000")
    assert store.ambiguous() == []
    assert store.status()[0]["state"] == "expired_ambiguous"
    assert store.held_payload("turn", "send", {"content": "uncertain"})
    assert store.pending() == []


@pytest.mark.asyncio
async def test_recovery_restores_image_pairs_and_control_parse_mode(tmp_path):
    store = Outbox(tmp_path)
    store.enqueue("images", "send_multiple_images", {
        "chat_id": "chat", "images": [("https://example.com/a.png", "alt")],
    })
    store.enqueue("control", "control_prompt", {
        "chat_id": "chat", "text": "Confirm", "parse_mode": "HTML",
        "thread_id": None, "metadata": None, "reply_markup": None, "reply_to_mode": None,
    })
    seen = []
    class Adapter:
        async def send_multiple_images(self, **kwargs):
            seen.append(kwargs["images"])
            return SendResult(True, message_id="image")
        async def _send_control_message(self, **kwargs):
            seen.append(kwargs["parse_mode"])
            return SimpleNamespace(message_id="control")
    assert await recover(store, Adapter()) == (2, 0)
    assert seen == [[("https://example.com/a.png", "alt")], "HTML"]


def test_additive_admission_timestamp_preserves_existing_receipt(tmp_path):
    path = tmp_path / "gateway-outbox.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE admissions (profile TEXT, platform TEXT, transport_event_id TEXT, "
                   "event_kind TEXT, turn_id TEXT, result TEXT, "
                   "PRIMARY KEY (profile, platform, transport_event_id, event_kind))")
        db.execute("INSERT INTO admissions VALUES ('default', 'telegram', 'event', 'message', 'turn', 'completed')")
    store = Outbox(tmp_path)
    assert store.lookup("default", "telegram", "event", "message") == ("turn", "completed")
    assert store.admit("default", "telegram", "new", "message")[1]
    with sqlite3.connect(path) as db:
        assert all(row[0] is not None for row in db.execute("SELECT created_at FROM admissions"))


def test_additive_schema_does_not_disturb_existing_store(tmp_path):
    path = tmp_path / "gateway-outbox.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE older_release (value TEXT)")
        db.execute("INSERT INTO older_release VALUES ('intact')")
    Outbox(tmp_path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM older_release").fetchone()[0] == "intact"


class _FinalAdapter(TelegramAdapter):
    def __init__(self, runner, sends):
        BasePlatformAdapter.__init__(self, PlatformConfig(enabled=True), Platform.TELEGRAM)
        self.gateway_runner = runner
        self.sends = sends
        self._bot = object()
        self._chat_send_lock = lambda chat: contextlib.nullcontext()
        self._start_typing_refresh = lambda *a, **kw: None
        self._stop_typing_refresh = AsyncMock()
        self._run_processing_hook = AsyncMock()
        self._fire_post_delivery_callback = AsyncMock()
        self._flush_text_debounce_now = AsyncMock()
        self._finish_session_task = lambda *a: None

    async def _send_text_locked(self, chat_id, content, reply_to, metadata):
        self.sends.append(content)
        return SendResult(True, message_id=str(len(self.sends)))


def _final_fixture(home, sends):
    class Runner(GatewayInboundMixin):
        async def _handle_admitted_message(self, event):
            admitted = await self._hm_admit_event(event)
            if event.text == "explode":
                raise RuntimeError("agent failed")
            return "answer" if admitted and not getattr(event, "_outbox_duplicate", False) else None

        async def _hm_pre_gateway_dispatch_hook(self, event, source):
            return event

        def _scale_to_zero_note_real_inbound(self):
            pass

        def _is_user_authorized_for_source(self, source):
            return True

        def _admit_bot_message_for_source(self, source):
            return True

        def _resolve_profile_home_for_source(self, source):
            return home

    runner = Runner()
    runner.config = SimpleNamespace(durable_outbox_enabled=True, multiplex_profiles=False)
    adapter = _FinalAdapter(runner, sends)
    adapter.set_message_handler(runner._handle_message)
    return adapter


def _final_event():
    return MessageEvent(text="hello", message_type=MessageType.TEXT, message_id="original",
                        source=SessionSource(platform=Platform.TELEGRAM, chat_id="chat", user_id="user"))


@pytest.mark.asyncio
@pytest.mark.parametrize("first_result", [None, "scheduled"])
async def test_native_same_event_replay_retains_admission_without_transport_redelivery(tmp_path, first_result):
    sends, calls, tasks = [], [], []
    adapter = _final_fixture(tmp_path, sends)
    runner = adapter.gateway_runner
    async def handler(event):
        admitted = await runner._hm_admit_event(event)
        if not admitted or getattr(event, "_outbox_duplicate", False):
            return None
        calls.append(event._outbox_turn_id)
        return first_result if len(calls) == 1 else "executed"
    runner._handle_admitted_message = handler
    adapter._finish_session_task = lambda key, guard: adapter._active_sessions.pop(key, None)
    def start(event, key):
        tasks.append(asyncio.create_task(adapter._process_message_background(event, key)))
        return True
    adapter._start_session_processing = start
    event = _final_event()
    await adapter.handle_message(event)
    await tasks[-1]
    await adapter.handle_message(event)
    if len(tasks) > 1:
        await tasks[-1]
    assert len(calls) == 2 and calls[0] == calls[1]
    assert sends == (["scheduled"] if first_result else []) + ["executed"]
    before = len(tasks)
    await adapter.handle_message(_final_event())  # Another object is a transport redelivery.
    assert len(tasks) == before
    rows = Outbox(tmp_path).all_rows()
    assert len(rows) == len(sends) and {row.turn_id for row in rows} == {calls[0]}


@pytest.mark.asyncio
async def test_local_replay_cannot_reuse_another_profiles_outbox_turn(tmp_path):
    homes = {"default": tmp_path / "a", "work": tmp_path / "b"}
    adapter = _final_fixture(homes["default"], [])
    runner = adapter.gateway_runner
    runner._resolve_profile_home_for_source = lambda source: homes[source.profile or "default"]
    event = _final_event()
    assert await runner._handle_message(event) == "answer"
    first_turn = event._outbox_turn_id
    event.source.profile = "work"
    assert await runner._handle_message(event) == "answer"
    assert event._outbox_turn_id != first_turn and event._outbox_home == homes["work"]
    event.source.profile = "default"
    assert await runner._handle_message(event) is None
    assert event._outbox_duplicate and event._outbox_home == homes["default"]


@pytest.mark.asyncio
async def test_runner_to_adapter_final_has_exactly_one_outbox_receipt(tmp_path):
    sends = []
    adapter = _final_fixture(tmp_path, sends)
    event = _final_event()
    await adapter._process_message_background(event, "session")
    rows = Outbox(tmp_path).all_rows()
    assert sends == ["answer"]
    assert len(rows) == 1 and rows[0].turn_id == event._outbox_turn_id
    assert rows[0].state == "delivered"
    assert active_turn() is None
    assert await recover(Outbox(tmp_path), adapter) == (0, 0)
    assert sends == ["answer"]


def _crash_final_before_ack(home):
    import gateway.outbox as outbox
    adapter = _final_fixture(home, [])
    def exit_after_enqueue(store, row):
        os._exit(0)
    outbox.Outbox.begin_send = exit_after_enqueue
    asyncio.run(adapter._process_message_background(_final_event(), "session"))


def test_completed_agent_final_recovers_after_crash_before_ack(tmp_path):
    child = mp.Process(target=_crash_final_before_ack, args=(tmp_path,))
    child.start()
    child.join(timeout=10)
    assert child.exitcode == 0
    sends = []
    adapter = _final_fixture(tmp_path, sends)
    assert asyncio.run(recover(Outbox(tmp_path), adapter)) == (1, 0)
    assert sends == ["answer"]
    assert asyncio.run(recover(Outbox(tmp_path), adapter)) == (0, 0)
    assert sends == ["answer"]


@pytest.mark.asyncio
async def test_live_retry_cannot_claim_fresh_pending_row(tmp_path, monkeypatch):
    store = Outbox(tmp_path)
    sends = []
    adapter = _fake_adapter(True, sends)
    original = store.begin_send
    def race(row):
        # A live retry wakes on another event loop after enqueue but before
        # this send claims its row; it must not dispatch the fresh payload.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as pool:
            attempt = pool.submit(lambda: asyncio.run(recover(store, adapter, startup=False)))
            assert attempt.result(timeout=5) == (0, 0)
        return original(row)
    monkeypatch.setattr(store, "begin_send", race)
    monkeypatch.setattr("gateway.outbox.store_for", lambda home: store)
    bind_turn(tmp_path, "race")
    try:
        assert (await adapter.send("chat", "answer")).success
        assert sends == [("chat", "answer")]
    finally:
        clear_turn()


@pytest.mark.asyncio
async def test_stream_final_deferral_has_one_durable_retry_no_fallback(tmp_path):
    from gateway.outbox import run_turn_child
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
    sends = []
    second_send = asyncio.Event()
    adapter = _fake_adapter(True, sends)
    async def limited(chat_id, content, reply_to, metadata):
        sends.append(content)
        if len(sends) == 1:
            return SendResult(False, error="flood_control:0.01", retry_after=0.01)
        second_send.set()
        return SendResult(True, message_id="final")
    adapter._send_text_locked = limited
    bind_turn(tmp_path, "stream-deferred")
    try:
        consumer = GatewayStreamConsumer(
            adapter, "chat", StreamConsumerConfig(buffer_only=True, cursor="", transport="edit"))
        task = asyncio.create_task(run_turn_child(consumer.run(), active_turn()))
        consumer.on_delta("answer")
        consumer.finish("answer")
        await task
        assert consumer.delivered_final_matches("answer")
        await asyncio.wait_for(second_send.wait(), timeout=10)
        assert sends == ["answer", "answer"]
        for _ in range(60):
            if [row.state for row in Outbox(tmp_path).all_rows()] == ["delivered"]:
                break
            await asyncio.sleep(0.05)
        assert [row.state for row in Outbox(tmp_path).all_rows()] == ["delivered"]
    finally:
        clear_turn()


def test_scalar_outbox_setting_reports_required_mapping():
    with pytest.raises(ValueError, match="durable_outbox.*mapping"):
        GatewayConfig.from_dict({"gateway": {"durable_outbox": True}})


@pytest.mark.asyncio
async def test_error_notice_after_handler_is_outbox_receipted(tmp_path):
    sends = []
    adapter = _final_fixture(tmp_path, sends)
    event = _final_event()
    event.text = "explode"
    await adapter._process_message_background(event, "session")
    rows = Outbox(tmp_path).all_rows()
    assert len(rows) == 1 and rows[0].turn_id == event._outbox_turn_id
    assert rows[0].state == "delivered"
    assert len(sends) == 1 and "encountered an error" in sends[0]


@pytest.mark.asyncio
async def test_ledger_covers_outbox_event_without_active_send_binding(tmp_path, monkeypatch):
    import gateway.delivery_ledger as ledger
    recorded = []
    monkeypatch.setattr(ledger, "ledger_enabled", lambda: True)
    monkeypatch.setattr(ledger, "record_obligation", lambda **kw: recorded.append(kw))
    monkeypatch.setattr(ledger, "mark_attempting", lambda obligation_id: None)
    adapter = _final_fixture(tmp_path, [])
    event = _final_event()
    event._outbox_turn_id = "admitted"
    event._outbox_home = tmp_path
    obligation = await adapter._record_delivery_obligation(event, "session", "answer", adapter, False)
    assert obligation is not None
    assert len(recorded) == 1 and recorded[0]["content"] == "answer"


@pytest.mark.asyncio
async def test_slow_receipt_does_not_block_other_coroutines(tmp_path, monkeypatch):
    store = Outbox(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    completed_while_waiting = []
    original = store.enqueue

    def slow_enqueue(*args, **kwargs):
        entered.set()
        completed_while_waiting.append(release.wait(timeout=3))
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "enqueue", slow_enqueue)
    monkeypatch.setattr("gateway.outbox.store_for", lambda home: store)
    adapter = _fake_adapter(True, [])
    bind_turn(tmp_path, "slow")
    try:
        async def pulse():
            while not entered.is_set():
                await asyncio.sleep(0.001)
            release.set()

        ticker = asyncio.create_task(pulse())
        sending = asyncio.create_task(_send_bound(adapter, tmp_path, "slow"))
        await asyncio.wait_for(sending, timeout=5)
        await ticker
        assert completed_while_waiting == [True]
    finally:
        release.set()
        clear_turn()


async def _send_bound(adapter, home, turn):
    bind_turn(home, turn)
    try:
        return await adapter.send("chat", "answer")
    finally:
        clear_turn()


@pytest.mark.asyncio
async def test_open_outbox_migration_does_not_block_event_loop(tmp_path, monkeypatch):
    from gateway.outbox import open_outbox
    entered, released = threading.Event(), threading.Event()
    original = Outbox._connect
    waits = []

    def slow_connect(self):
        if not entered.is_set():
            entered.set()
            waits.append(released.wait(timeout=2))
        return original(self)

    monkeypatch.setattr(Outbox, "_connect", slow_connect)

    async def pulse():
        await asyncio.to_thread(entered.wait, 2)
        released.set()

    store, _ = await asyncio.gather(open_outbox(tmp_path), pulse())
    assert store.path.is_file() and waits == [True]


def test_prune_retains_unresolved_and_recent_rows_and_removes_terminal_media(tmp_path):
    store = Outbox(tmp_path)
    media = tmp_path / "gateway-outbox-media" / "copy.txt"
    media.parent.mkdir()
    media.write_text("owned")
    old = time.time() - 9 * 86400
    delivered = store.enqueue("old", "send_document", {
        "file_path": str(media), "_outbox_original": {"file_path": "source"}})
    store.begin_send(delivered)
    store.receipt(delivered, message_id="one", success=True)
    failed_media = tmp_path / "gateway-outbox-media" / "failed.txt"
    failed_media.write_text("failed-owned")
    failed = store.enqueue("old", "send_document", {
        "file_path": str(failed_media), "_outbox_original": {"file_path": "source2"}})
    store.begin_send(failed)
    store.receipt(failed, message_id=None, success=False, uncertain=False)
    ambiguous = store.enqueue("old", "send", {"content": "uncertain"})
    store.begin_send(ambiguous)
    pending = store.enqueue("old", "send", {"content": "pending"})
    recent = store.enqueue("recent", "send", {"content": "recent"})
    old_turn, _ = store.admit("default", "telegram", "old", "message")
    store.finish_admission(old_turn, "completed")
    recent_turn, _ = store.admit("default", "telegram", "recent", "message")
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET created_at=? WHERE turn_id='old'", (old,))
        db.execute("UPDATE admissions SET created_at=? WHERE turn_id=?", (old, old_turn))
    store.prune(retention_days=7)
    assert {r.idempotency_key for r in store.all_rows()} == {
        ambiguous.idempotency_key, pending.idempotency_key, recent.idempotency_key}
    assert not media.exists() and not failed_media.exists()
    assert store.original_result(old_turn) is None
    assert store.lookup("default", "telegram", "recent", "message") == (recent_turn, None)


def test_structured_transport_error_takes_precedence_over_message():
    assert _uncertain(SendResult(False, error="timeout", error_kind="not_found")) is False
    assert _uncertain(SendResult(False, error="connection reset", error_kind="transient")) is True
    assert _uncertain(SendResult(False, error="connection reset", error_kind="transient", retry_after=2)) is False
    assert _uncertain(SendResult(False, error="timeout", error_kind="unknown")) is True
    assert _uncertain(SendResult(False, error="timeout", raw_response={"ok": False})) is False


@pytest.mark.asyncio
async def test_scheduled_retry_uses_reconnected_adapter(tmp_path):
    original_sends, replacement_sends = [], []
    old = _fake_adapter(True, original_sends)
    current = _fake_adapter(True, replacement_sends)
    delivered = asyncio.Event()
    original_send = current._send_text_locked
    async def notified(*args):
        result = await original_send(*args)
        delivered.set()
        return result
    current._send_text_locked = notified
    registry = {Platform.TELEGRAM: old}
    old.gateway_runner.adapters = registry
    current.gateway_runner.adapters = registry

    async def rate_limited(chat_id, content, reply_to, metadata):
        original_sends.append((chat_id, content))
        return SendResult(False, error="rate limited", retry_after=0.15)

    old._send_text_locked = rate_limited
    bind_turn(tmp_path, "reconnected")
    try:
        assert (await old.send("chat", "answer")).deferred
        registry[Platform.TELEGRAM] = current
        await asyncio.wait_for(delivered.wait(), timeout=3)
        for _ in range(100):
            if [row.state for row in Outbox(tmp_path).all_rows()] == ["delivered"]:
                break
            await asyncio.sleep(0.01)
        assert original_sends == [("chat", "answer")]
        assert replacement_sends == [("chat", "answer")]
        assert [row.state for row in Outbox(tmp_path).all_rows()] == ["delivered"]
    finally:
        clear_turn()


@pytest.mark.asyncio
async def test_retry_deadline_passed_during_scheduling_is_dispatched(tmp_path, monkeypatch):
    sends = []
    store = Outbox(tmp_path)
    original_scheduled = store.scheduled

    def slow_scheduled():
        time.sleep(0.06)
        return original_scheduled()

    monkeypatch.setattr(store, "scheduled", slow_scheduled)
    monkeypatch.setattr("gateway.outbox.store_for", lambda home: store)
    adapter = _fake_adapter(True, sends)
    completed = asyncio.Event()

    async def rate_limited(chat_id, content, reply_to, metadata):
        sends.append(content)
        if len(sends) == 1:
            return SendResult(False, error="flood_control", retry_after=0.01)
        completed.set()
        return SendResult(True, message_id="delivered")

    adapter._send_text_locked = rate_limited
    bind_turn(tmp_path, "overdue")
    try:
        result = await adapter.send("chat", "answer")
        assert result.success and result.deferred
        await asyncio.wait_for(completed.wait(), timeout=3)
        assert sends == ["answer", "answer"]
    finally:
        clear_turn()


@pytest.mark.asyncio
async def test_boot_sweep_uses_profile_retention(tmp_path):
    (tmp_path / "config.yaml").write_text("gateway:\n  durable_outbox:\n    retention_days: 2\n")
    store = Outbox(tmp_path)
    row = store.enqueue("old", "send", {"content": "sent"})
    store.begin_send(row)
    store.receipt(row, message_id="one", success=True)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET created_at=?", (time.time() - 3 * 86400,))
    adapter = _fake_adapter(True, [])
    adapter.gateway_runner.config.durable_outbox_retention_days = 7
    assert await recover(store, adapter) == (0, 0)
    assert store.all_rows() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["0", "true", "'7'"])
async def test_boot_sweep_invalid_profile_retention_uses_default(tmp_path, caplog, bad):
    (tmp_path / "config.yaml").write_text(f"gateway:\n  durable_outbox:\n    retention_days: {bad}\n")
    store = Outbox(tmp_path)
    row = store.enqueue("old", "send", {"content": "sent"})
    store.begin_send(row)
    store.receipt(row, message_id="one", success=True)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET created_at=?", (time.time() - 3 * 86400,))
    assert await recover(store, _fake_adapter(True, [])) == (0, 0)
    assert store.all_rows() == [row.__class__(**{**row.__dict__, "state": "delivered", "message_id": "one"})]
    assert "retention" in caplog.text


@pytest.mark.asyncio
async def test_boot_sweep_honors_legacy_retention(tmp_path):
    (tmp_path / "gateway.json").write_text('{"durable_outbox":{"retention_days":2}}')
    store = Outbox(tmp_path)
    row = store.enqueue("old", "send", {"content": "sent"})
    store.begin_send(row)
    store.receipt(row, message_id="one", success=True)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET created_at=?", (time.time() - 3 * 86400,))
    assert await recover(store, _fake_adapter(True, [])) == (0, 0)
    assert store.all_rows() == []


def test_retention_config_parses_and_rejects_invalid_age():
    config = GatewayConfig.from_dict({"gateway": {"durable_outbox": {
        "enabled": True, "retention_days": 3}}})
    assert config.durable_outbox_retention_days == 3
    assert config.to_dict()["durable_outbox"]["retention_days"] == 3
    for age in (0, -1, True, 1.5, "7"):
        with pytest.raises(ValueError, match="retention_days"):
            GatewayConfig.from_dict({"gateway": {"durable_outbox": {"retention_days": age}}})

def test_old_outbox_schema_migrates_without_losing_receipts(tmp_path):
    path = tmp_path / "gateway-outbox.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE outbox (turn_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
                   "type TEXT NOT NULL, payload TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, "
                   "owner_epoch INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending' "
                   "CHECK (state IN ('pending','sending','ambiguous','delivered')), message_id TEXT, "
                   "send_status TEXT, edit_status TEXT, PRIMARY KEY (turn_id, sequence))")
        db.execute("INSERT INTO outbox VALUES ('old',1,'send','{}','old-key',0,'pending',NULL,NULL,NULL)")
    store = Outbox(tmp_path)
    Outbox(tmp_path)
    assert store.all_rows()[0].idempotency_key == "old-key"
    assert store.begin_send(store.all_rows()[0])
    store.receipt(store.all_rows()[0], message_id=None, success=False, uncertain=False)
    assert store.all_rows()[0].state == "failed_unsent"

def test_concurrent_old_admission_migration_preserves_single_receipt(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / "gateway-outbox.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE admissions (profile TEXT, platform TEXT, transport_event_id TEXT, "
                   "event_kind TEXT, turn_id TEXT, result TEXT, "
                   "PRIMARY KEY (profile, platform, transport_event_id, event_kind))")
        db.execute("INSERT INTO admissions VALUES ('default','telegram','event','message','turn','completed')")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: Outbox(tmp_path).lookup("default", "telegram", "event", "message"), range(8)))
    assert results == [("turn", "completed")] * 8

@pytest.mark.asyncio
async def test_cancelled_store_io_preserves_cancellation_when_disk_fails():
    entered, release = threading.Event(), threading.Event()
    def fails():
        entered.set()
        release.wait(timeout=3)
        raise OSError("disk failed")
    task = asyncio.create_task(_store_io(fails))
    await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

@pytest.mark.parametrize("error", [ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"),
                                         OSError(errno.ECONNREFUSED, "Connection refused")])
def test_connection_refused_is_definitively_unsent(error):
    from gateway.platforms.base import classify_send_error
    result = SendResult(False, error=str(error), error_kind=classify_send_error(error), retryable=True)
    assert not _uncertain(result)


def test_connection_reset_is_held_after_classification():
    from gateway.platforms.base import classify_send_error
    error = ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
    assert _uncertain(SendResult(False, error=str(error), error_kind=classify_send_error(error), retryable=True))

def test_prune_bounds_expired_ambiguous_and_unfinished_admissions(tmp_path):
    store = Outbox(tmp_path)
    media = tmp_path / "gateway-outbox-media" / "held.txt"
    media.parent.mkdir()
    media.write_text("held")
    row = store.enqueue("held", "send_document", {"file_path": str(media), "_outbox_original": {"file_path": "source"}})
    store.begin_send(row)
    unfinished, _ = store.admit("default", "telegram", "unfinished", "message")
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET state='expired_ambiguous', created_at=?", (time.time() - 29 * 86400,))
        db.execute("UPDATE admissions SET created_at=?", (time.time() - 29 * 86400,))
    store.prune(retention_days=7)
    assert store.all_rows() == [] and not media.exists()
    assert store.original_result(unfinished) is None

def test_prune_unlinks_media_only_after_row_deletion_is_committed(tmp_path, monkeypatch):
    import gateway.outbox as outbox
    store = Outbox(tmp_path)
    media = tmp_path / "gateway-outbox-media" / "copy.txt"
    media.parent.mkdir()
    media.write_text("owned")
    row = store.enqueue("old", "send_document", {"file_path": str(media),
                        "_outbox_original": {"file_path": "original"}})
    store.begin_send(row)
    store.receipt(row, message_id="delivered", success=True)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET created_at=?", (time.time() - 9 * 86400,))
    original = outbox._discard_delivered_media
    def after_commit(home, payload):
        with sqlite3.connect(store.path) as db:
            assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
        original(home, payload)
    monkeypatch.setattr(outbox, "_discard_delivered_media", after_commit)
    store.prune(retention_days=7)
    assert not media.exists()

@pytest.mark.asyncio
async def test_scheduled_recovery_does_not_prune(tmp_path, monkeypatch):
    store = Outbox(tmp_path)
    monkeypatch.setattr(store, "prune", lambda **kw: pytest.fail("scheduled retry pruned"))
    assert await recover(store, _fake_adapter(True, []), startup=False) == (0, 0)

@pytest.mark.asyncio
async def test_boot_sweep_uses_store_profile_not_launch_retention(tmp_path):
    home = tmp_path / "satellite"
    home.mkdir()
    (home / "config.yaml").write_text("gateway:\n  durable_outbox:\n    retention_days: 2\n")
    store = Outbox(home)
    row = store.enqueue("old", "send", {"content": "sent"})
    store.begin_send(row)
    store.receipt(row, message_id="one", success=True)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET created_at=?", (time.time() - 3 * 86400,))
    adapter = _fake_adapter(True, [])
    adapter.gateway_runner.config.durable_outbox_retention_days = 7
    assert await recover(store, adapter) == (0, 0)
    assert store.all_rows() == []


async def _retry_with_instant_backoff(adapter, content):
    from unittest.mock import patch
    with patch("gateway.platforms.base.asyncio.sleep", new=AsyncMock()):
        return await adapter._send_with_retry("chat", content)


@pytest.mark.asyncio
async def test_degraded_refusal_is_unsent_and_retry_delivers_once(tmp_path):
    """A send refused before any request must not hold the retry behind an 'uncertain' row."""
    sends = []
    adapter = _fake_adapter(True, sends)
    adapter._send_path_degraded = True

    async def recover_during_backoff(*_a, **_kw):
        adapter._send_path_degraded = False

    bind_turn(tmp_path, "degraded")
    try:
        from unittest.mock import patch
        with patch("gateway.platforms.base.asyncio.sleep", new=AsyncMock(side_effect=recover_during_backoff)):
            result = await adapter._send_with_retry("chat", "final")
        assert result.success
        assert sends == [("chat", "final")]
        assert [row.state for row in Outbox(tmp_path).all_rows()] == ["failed_unsent", "delivered"]
    finally:
        clear_turn()


@pytest.mark.asyncio
async def test_outbox_hold_is_final_without_plain_text_copy(tmp_path):
    """An uncertain first dispatch holds the retry; no banner-prefixed copy may slip past as a new payload."""
    sends = []
    adapter = _fake_adapter(True, sends)

    async def maybe_landed(chat_id, content, reply_to, metadata):
        sends.append(content)
        return SendResult(False, error="network timeout", retryable=True)

    adapter._send_text_locked = maybe_landed
    bind_turn(tmp_path, "held")
    try:
        result = await _retry_with_instant_backoff(adapter, "final")
        assert not result.success and result.held
        assert sends == ["final"]
        assert [row.state for row in Outbox(tmp_path).all_rows()] == ["ambiguous"]
    finally:
        clear_turn()


@pytest.mark.asyncio
async def test_refused_outbox_final_is_ledgered_for_reconnect_redelivery(tmp_path, monkeypatch):
    """The outbox never replays a proven-unsent final, so the delivery ledger must own it."""
    from gateway import delivery_ledger as ledger
    monkeypatch.setattr(ledger, "_db_path", lambda: tmp_path / "state.db")
    sends = []
    adapter = _final_fixture(tmp_path, sends)
    adapter._send_path_degraded = True
    event = _final_event()
    event._outbox_home, event._outbox_turn_id = tmp_path, "refused-final"
    bind_turn(tmp_path, "refused-final")
    try:
        from unittest.mock import patch
        with patch("gateway.platforms.base.asyncio.sleep", new=AsyncMock()):
            result, _ = await adapter.send_final_ledgered(event, "session", "answer", {}, reply_to=None)
    finally:
        clear_turn()
    assert not result.success and result.pre_send and sends == []
    assert {row.state for row in Outbox(tmp_path).all_rows()} == {"failed_unsent"}
    claimed = ledger.sweep_failed_for_runtime("telegram")
    assert [row["content"] for row in claimed] == ["answer"]
