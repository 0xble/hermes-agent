"""Partial-send receipts survive failure settlement and durable recovery."""
import sqlite3
import time
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway import delivery_ledger as dl
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run_startup import GatewayStartupMixin
from gateway.session import SessionSource
from tests.gateway.test_send_retry import _StubAdapter
from plugins.platforms.telegram.adapter import TelegramAdapter, _TelegramSendCooldownExceeded


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(dl, "_owner_stamp", lambda: (101, 202))
    monkeypatch.setattr(dl, "_owner_alive", lambda *_: False)
    monkeypatch.setattr("gateway.platforms.base.asyncio.sleep", AsyncMock())


def _row():
    with sqlite3.connect(dl._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM delivery_obligations").fetchone())


def _partial(suffix, *, wait=999):
    failure = SendResult(success=False, error=f"flood_control:{wait}", retry_after=wait)
    return TelegramAdapter._partial_text_delivery_failure(
        failure, ["ack-prefix"], ["acknowledged prefix ", suffix])


async def _produce(adapter, text="prefix middle tail"):
    event = MessageEvent(text="question", message_type=MessageType.TEXT,
                         source=SessionSource(platform=Platform.TELEGRAM, chat_id="123"),
                         message_id="inbound-1")
    return await adapter.send_final_ledgered(event, "telegram:123", text, {}, reply_to=None)


class _Replay(GatewayStartupMixin):
    def __init__(self, adapter):
        self.adapters = {Platform.TELEGRAM: adapter}
        self._authorization_adapter = lambda platform, profile: self.adapters.get(platform)
        self._arm_flood_timers_for_waiting_rows = AsyncMock()
        self._consume_delivered_goal_receipt = AsyncMock()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", [False, True])
@pytest.mark.parametrize("intermediate_failure", [False, True])
async def test_real_telegram_wire_suffix_survives_durable_replay(monkeypatch, runtime, intermediate_failure):
    import plugins.platforms.telegram.adapter as adapter_module

    assert Path(adapter_module.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[2])
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._rich_messages_enabled = False
    adapter._send_cooldown_max_wait = 0.01
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(side_effect=[
        SimpleNamespace(message_id=101), _TelegramSendCooldownExceeded(999),
    ])

    async def immediate_send(chat_id, send_fn, *args, **kwargs):
        return await send_fn(*args, **kwargs)

    monkeypatch.setattr(adapter, "_run_send_call", immediate_send)
    result, _ = await _produce(adapter, "x" * adapter.MAX_MESSAGE_LENGTH + "\n\n**bold**.")
    assert not result.success
    refused_wire = adapter._bot.send_message.await_args_list[1].kwargs["text"]
    assert "*bold*" in refused_wire
    assert _row()["content"] == refused_wire
    assert json.loads(_row()["retry_payload"])["chunks"] == [refused_wire]
    replay = _Replay(adapter)

    def claim():
        now = time.time() + 1000
        return dl.sweep_failed_for_runtime("telegram", now=now) if runtime else dl.sweep_recoverable(now=now)

    if intermediate_failure:
        adapter._send_path_degraded = True
        assert await replay._redeliver_claimed_obligations(claim()) == 0
        assert _row()["content"] == refused_wire
        assert json.loads(_row()["retry_payload"])["chunks"] == [refused_wire]
        adapter._send_path_degraded = False

    adapter._bot.send_message.reset_mock()
    adapter._bot.send_message.side_effect = None
    adapter._bot.send_message.return_value = SimpleNamespace(message_id=102)
    assert await replay._redeliver_claimed_obligations(claim()) == 1
    attempts = adapter._bot.send_message.await_args_list
    assert len(attempts) == 2  # separately formatted recovery notice, then exact suffix
    assert "Recovered reply" in attempts[0].kwargs["text"]
    assert attempts[1].kwargs["text"] == refused_wire
    assert _row()["state"] == "delivered"


@pytest.mark.asyncio
@pytest.mark.parametrize("inline_suffix_failure", [False, True])
async def test_initial_and_recovered_partial_sends_persist_only_remaining_payload(inline_suffix_failure):
    adapter = _StubAdapter()
    adapter._send_results = [_partial("middle tail", wait=0 if inline_suffix_failure else 999)]
    if inline_suffix_failure:
        # The suffix attempt itself establishes no further progress and supplies no metadata.
        adapter._send_results.append(SendResult(success=False, error="flood_control:999", retry_after=999))
    result, _ = await _produce(adapter)
    assert not result.success
    assert _row()["content"] == "middle tail"
    assert _row()["state"] == "failed"
    assert result.raw_response["delivery_retry_content"] == "middle tail"
    assert [text for _, text in adapter._send_calls] == [
        "prefix middle tail", *(["middle tail"] if inline_suffix_failure else [])]

    # Startup recovery uses a real durable claim and the production dispatch/settlement path.
    replay = _Replay(adapter)
    claimed = dl.sweep_recoverable(now=time.time() + 1000)
    adapter._send_results = [_partial("tail")]
    assert await replay._redeliver_claimed_obligations(claimed) == 0
    assert adapter._send_calls[-1][1].endswith("middle tail")
    assert "prefix" not in adapter._send_calls[-1][1]
    assert _row()["content"] == "tail"
    assert _row()["state"] == "failed"
    claimed = dl.sweep_recoverable(now=time.time() + 1000)
    adapter._send_results = [SendResult(success=True, message_id="ack-tail")]
    assert await replay._redeliver_claimed_obligations(claimed) == 1
    assert adapter._send_calls[-1][1].endswith("tail")
    assert "middle" not in adapter._send_calls[-1][1]
    assert _row()["state"] == "delivered"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["producer", "replay"])
@pytest.mark.parametrize("competing_state", ["attempting", "delivered"])
@pytest.mark.parametrize("new_owner", [False, True])
async def test_stale_partial_failure_cannot_overwrite_competing_owner(phase, competing_state, new_owner):
    adapter = _StubAdapter()
    original_send = adapter.send

    async def stale_send(*args, **kwargs):
        # A separate DB connection represents another recovery owner taking the row.
        with sqlite3.connect(dl._db_path()) as conn:
            conn.execute("UPDATE delivery_obligations SET owner_pid=?, owner_started_at=?, "
                         "state=?, content='new owner payload'",
                         (303 if new_owner else 101, 404 if new_owner else 202, competing_state))
        await original_send(*args, **kwargs)
        return _partial("stale suffix")

    if phase == "producer":
        adapter.send = stale_send
        await _produce(adapter)
    else:
        adapter._send_results = [_partial("middle tail")]
        await _produce(adapter)
        claimed = dl.sweep_recoverable(now=time.time() + 1000)
        adapter.send = stale_send
        await _Replay(adapter)._redeliver_claimed_obligations(claimed)
    assert _row()["content"] == "new owner payload"
    assert _row()["state"] == competing_state
    assert _row()["owner_pid"] == (303 if new_owner else 101)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["request timed out", "flood_control:999"])
async def test_failure_without_positive_suffix_receipt_keeps_full_payload(error):
    adapter = _StubAdapter()
    adapter._send_results = [SendResult(success=False, error=error,
                                       retry_after=999 if error.startswith("flood") else None)]
    result, _ = await _produce(adapter)
    assert not result.success
    assert len(adapter._send_calls) == 1
    assert _row()["content"] == "prefix middle tail"
    assert _row()["state"] == "failed"


@pytest.mark.asyncio
async def test_suffix_timeout_is_not_retried_or_used_to_infer_more_progress():
    adapter = _StubAdapter()
    adapter._send_results = [_partial("middle tail", wait=0),
                             SendResult(success=False, error="request timed out")]
    result, _ = await _produce(adapter)
    assert not result.success
    assert [text for _, text in adapter._send_calls] == ["prefix middle tail", "middle tail"]
    assert _row()["content"] == "middle tail"


@pytest.mark.asyncio
async def test_formatting_fallback_preserves_established_suffix():
    adapter = _StubAdapter()
    adapter._send_results = [_partial("middle tail", wait=0),
                             SendResult(success=False, error="invalid formatting"),
                             SendResult(success=False, error="flood_control:999", retry_after=999)]
    result, _ = await _produce(adapter)
    assert not result.success
    assert len(adapter._send_calls) == 3
    assert "prefix" not in adapter._send_calls[-1][1]
    assert _row()["content"] == "middle tail"


@pytest.mark.asyncio
async def test_terminal_partial_failure_stays_terminal_without_more_attempts():
    adapter = _StubAdapter()
    refusal = _partial("middle tail")
    refusal.error = "forbidden"
    refusal.retry_after = None
    adapter._send_results = [refusal]
    adapter._send_retry_is_final = lambda result: result.error == "forbidden"
    result, _ = await _produce(adapter)
    assert not result.success
    assert len(adapter._send_calls) == 1
    assert _row()["content"] == "middle tail"
    assert dl.sweep_failed_for_runtime("telegram", now=time.time() + 1000) == []
