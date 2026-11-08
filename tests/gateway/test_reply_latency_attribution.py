"""Inherited turn context cannot attribute unrelated transport confirmations."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from gateway.reply_latency import bind_agent_start, current_receipt, delivery_scope
from tests.gateway.test_reply_latency_logs import event, records
from tests.gateway.test_telegram_progress_literals import _telegram


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("destination", ["chat", "thread", "same"])
@pytest.mark.parametrize("path", ["send", "rich", "overflow"])
async def test_worker_send_to_another_destination_cannot_stamp_turn(caplog, scoped, destination, path):
    from model_tools import _run_async
    from tools.send_message_tool import _dispatch_on_gateway_loop

    caplog.set_level("INFO", logger="gateway.reply_latency")
    adapter = _telegram("always" if path == "rich" else "never")
    incoming = event(adapter, "inbound", "77")
    bind_agent_start(incoming)
    receipt = current_receipt()
    runner = SimpleNamespace(_gateway_loop=asyncio.get_running_loop())
    chat = "other-chat" if destination == "chat" else "chat"
    metadata = {"thread_id": "88" if destination == "thread" else "77"}

    def worker():
        make_coro = (lambda: adapter._edit_overflow_split(
            chat, "123", "word " * 1500, finalize=False, metadata=metadata)) if path == "overflow" else (
                lambda: adapter.send(chat, "unrelated", metadata=metadata))
        return _run_async(_dispatch_on_gateway_loop(runner, make_coro, "test send"))

    with delivery_scope(receipt, "text_send") if scoped else nullcontext():
        result = await asyncio.to_thread(worker)
    assert result.success
    if scoped and destination == "same":
        assert len([r for r in records(caplog) if r["event"] == "first_reply"]) == 1
        assert result.raw_response["reply_latency_confirmed_at"] == receipt.last_delivery_at
        return
    assert not [r for r in records(caplog) if r["event"] == "first_reply"]
    assert receipt.last_delivery_at is None
    assert "reply_latency_confirmed_at" not in (result.raw_response or {})


def test_receipt_log_directory_is_created_once(monkeypatch, tmp_path):
    from pathlib import Path
    from gateway.reply_latency import record_admitted

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    directory = tmp_path / "logs" / "reply-latency"
    directory.parent.mkdir()
    mkdir = Path.mkdir
    creations = []

    def counted(path, *args, **kwargs):
        if path == directory:
            creations.append(path)
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", counted)
    incoming = event(_telegram("never"), "inbound")
    bind_agent_start(incoming)
    record_admitted(incoming)
    assert len(creations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("same_destination", [False, True])
async def test_outbox_sweep_cannot_borrow_scheduling_turn_receipt(caplog, tmp_path, same_destination):
    from gateway import outbox

    caplog.set_level("INFO", logger="gateway.reply_latency")
    adapter = _telegram("never")
    bind_agent_start(event(adapter, "inbound", "77"))
    receipt = current_receipt()
    store = outbox.Outbox(tmp_path)
    row = store.enqueue("another-turn", "send", {
        "chat_id": "chat" if same_destination else "other-chat", "content": "old reply",
        "metadata": {"thread_id": "77"},
    })
    assert store.begin_send(row)
    assert store.receipt(row, message_id=None, success=False, uncertain=False, retry_after=0.0)
    with delivery_scope(receipt, "text_send"):
        await outbox._schedule_retry(store, adapter)
    key = (store.identity, adapter.platform, None)
    task, _ = outbox._SWEEPS[key]
    await asyncio.wait_for(task, timeout=5)
    assert store.pending() == []
    assert store.all_rows()[0].state == "delivered"
    adapter._bot.send_message.assert_awaited_once()
    assert not [r for r in records(caplog) if r["event"] == "first_reply"]
    assert receipt.last_delivery_at is None
