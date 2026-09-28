"""S4.1 durable admission and outbound delivery boundary."""

import asyncio
import contextlib
import multiprocessing as mp
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gateway.outbox import Outbox, active_turn, bind_turn, clear_turn, recover
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
    async def limited(chat_id, content, reply_to, metadata):
        sends.append(content)
        if len(sends) == 1:
            return SendResult(False, error="flood_control:0.01", retry_after=0.01)
        return SendResult(True, message_id="one-final")
    adapter._send_text_locked = limited
    bind_turn(tmp_path, "single-final")
    try:
        result = await adapter._send_with_retry("chat", "answer", max_retries=2, base_delay=0)
        assert result.success and result.deferred
        assert [r.state for r in Outbox(tmp_path).all_rows()] == ["pending"]
        await asyncio.sleep(0.1)
        assert sends == ["answer", "answer"]
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


def test_schema_upgrades_old_outbox_without_losing_receipts(tmp_path):
    path = tmp_path / "gateway-outbox.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE outbox (turn_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
                   "type TEXT NOT NULL, payload TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL, "
                   "owner_epoch INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending' "
                   "CHECK (state IN ('pending','sending','ambiguous','delivered')), "
                   "message_id TEXT, send_status TEXT, edit_status TEXT, "
                   "PRIMARY KEY (turn_id, sequence))")
        db.execute("INSERT INTO outbox VALUES ('t', 1, 'send', '{}', 'old', 0, "
                   "'delivered', 'm1', 'success', NULL)")
    store = Outbox(tmp_path)
    assert store.all_rows()[0].message_id == "m1"
    second = store.enqueue("t", "send", {"content": "no"})
    assert store.begin_send(second)
    store.receipt(second, message_id=None, success=False, uncertain=False)
    assert store.all_rows()[-1].state == "failed_unsent"


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
    adapter = _fake_adapter(True, sends)
    async def limited(chat_id, content, reply_to, metadata):
        sends.append(content)
        if len(sends) == 1:
            return SendResult(False, error="flood_control:0.01", retry_after=0.01)
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
        await asyncio.sleep(0.1)
        assert sends == ["answer", "answer"]
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
