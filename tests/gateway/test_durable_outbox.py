"""S4.1 durable admission and outbound delivery boundary."""

import asyncio
import contextlib
import multiprocessing as mp
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.outbox import Outbox, bind_turn, clear_turn, recover
from gateway.platforms.base import SendResult
from gateway.config import GatewayConfig, load_gateway_config
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
    assert not store.begin_send(second)
    assert store.begin_send(first)
    assert store.pending() == []
    store.receipt(first, message_id="msg-1", success=True)
    assert store.pending() == [second]
    assert store.begin_send(second)
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


def test_additive_schema_does_not_disturb_existing_store(tmp_path):
    path = tmp_path / "gateway-outbox.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE older_release (value TEXT)")
        db.execute("INSERT INTO older_release VALUES ('intact')")
    Outbox(tmp_path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM older_release").fetchone()[0] == "intact"
