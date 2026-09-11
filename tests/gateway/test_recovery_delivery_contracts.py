"""Recovery contracts at durable ledger and live watcher boundaries."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import delivery_ledger as ledger
from gateway.run_notifications import GatewayNotificationsMixin
from gateway.run_turn_runner import TurnRunner


@pytest.mark.parametrize("mode", ["startup", "runtime", "adopted"])
def test_recovered_rows_keep_delegation_receipt(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(ledger, "_db_path", lambda: tmp_path / "state.db")
    receipt = {"threads": ["thread-a"], "generation": 3}
    ledger.record_obligation(obligation_id="receipt", session_key="s", platform="telegram",
                             chat_id="42", thread_id=None, content="done",
                             adapter_profile="default", delegation_receipt=receipt)
    if mode != "startup":
        ledger.mark_failed("receipt", "flood_control:60" if mode == "adopted" else "send_path_degraded")
    if mode != "runtime":
        monkeypatch.setattr(ledger, "_owner_alive", lambda *_: False)
        rows = ledger.sweep_recoverable(now=time.time())
    else:
        rows = ledger.sweep_failed_for_runtime("telegram", profile="default")
    assert len(rows) == 1
    assert json.loads(rows[0]["delegation_receipt"]) == receipt
    if mode == "adopted":
        assert rows[0]["adopted"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_phase", ["cards", "legacy", "exhausted"])
async def test_optional_startup_failure_still_drains_notifications(monkeypatch, failed_phase):
    from gateway import delegation_cards, review_status_migration
    from tools.process_registry import process_registry
    import queue
    monkeypatch.setattr(process_registry, "completion_queue", queue.Queue())
    monkeypatch.setattr("tools.async_delegation.retry_current_owner_terminal_checkpoints", lambda _: 0)
    phases = {key: AsyncMock(side_effect=OSError("temporary failure") if key == failed_phase else None)
              for key in ("cards", "legacy", "exhausted")}
    monkeypatch.setattr(delegation_cards, "cards_for", lambda _: SimpleNamespace(reconcile=phases["cards"]))
    monkeypatch.setattr(review_status_migration, "retire_legacy_review_statuses", phases["legacy"])
    runner = SimpleNamespace(_running=True, _recover_ready_async_delegation_deliveries=phases["exhausted"])
    async def drain(_):
        runner._running = False
    runner._drain_watch_notifications = AsyncMock(side_effect=drain)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    await GatewayNotificationsMixin._async_delegation_watcher(runner)
    runner._drain_watch_notifications.assert_awaited_once()
    for phase in phases.values():
        phase.assert_awaited_once()


@pytest.mark.asyncio
async def test_edit_wait_rechecks_sibling_flood_deadline(monkeypatch):
    now = [100.0]
    deadlines = {}
    adapter = SimpleNamespace(edit_message=AsyncMock())
    st = SimpleNamespace(edit_clock_key="chat", last_edit_ts=100.0, adapter=adapter)
    runner = object.__new__(TurnRunner)
    runner._runner = SimpleNamespace(_progress_edit_retry_deadlines=deadlines, _progress_edit_clock={})
    runner._ctx = SimpleNamespace(source=SimpleNamespace(chat_id="42"))
    monkeypatch.setattr("gateway.run_turn_runner.time.monotonic", lambda: now[0])
    async def sleep(delay):
        now[0] += delay
        deadlines["chat"] = now[0] + 60
    monkeypatch.setattr(asyncio, "sleep", sleep)
    result = await runner._edit_progress_message(st, "1", "text")
    assert result.retry_after == 60
    assert not result.success
    adapter.edit_message.assert_not_awaited()
