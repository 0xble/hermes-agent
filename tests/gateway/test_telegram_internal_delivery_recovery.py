"""A confirmed in-place Telegram polling recovery drains only its owner's failed answers."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway import delivery_ledger as ledger
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "_db_path", lambda: tmp_path / "state.db")


def _adapter(profile):
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter._owner_profile = profile
    adapter._fatal_error_code = None
    adapter._fatal_error_message = None
    adapter._polling_teardown_started = False
    adapter._polling_progress_accepting = True
    adapter._polling_generation = 2
    adapter._polling_progress_event = asyncio.Event()
    adapter._polling_network_error_count = 1
    adapter._polling_conflict_count = 0
    adapter._polling_conflict_recovery_generation = None
    adapter._send_path_degraded = True
    adapter._running = True
    adapter._background_tasks = set()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="sent"))
    return adapter


def _runner(primary, secondary):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: primary}
    runner._profile_adapters = {"secondary": {Platform.TELEGRAM: secondary}}
    runner._active_profile_name = lambda: "default"
    store = MagicMock()
    store.clear_resume_pending = AsyncMock()
    store._store = None
    runner.session_store = None
    runner._async_session_store = store
    return runner


def _failed_row(oid, profile="default", error="send_path_degraded"):
    ledger.record_obligation(
        obligation_id=oid,
        session_key=f"agent:{profile}:telegram:private:42",
        platform="telegram",
        chat_id="42",
        thread_id=None,
        content=f"answer {oid}",
        adapter_profile=profile,
    )
    ledger.mark_attempting(oid)
    ledger.mark_failed(oid, error)


def _state(oid):
    with ledger._connect() as conn:
        return conn.execute(
            "SELECT state, attempts FROM delivery_obligations WHERE obligation_id=?",
            (oid,),
        ).fetchone()


@pytest.mark.asyncio
async def test_polling_health_replays_only_owner_failed_rows_once():
    primary, secondary = _adapter("default"), _adapter("secondary")
    runner = _runner(primary, secondary)
    primary.gateway_runner = secondary.gateway_runner = runner
    _failed_row("primary")
    _failed_row("secondary", "secondary")
    _failed_row("permanent", error="chat_not_found")
    _failed_row("already-delivered")
    ledger.mark_delivered("already-delivered")

    with patch.object(primary, "_write_runtime_status_safe"):
        assert primary._record_polling_progress(1) is False  # stale generation
        assert primary._record_polling_progress(2) is True
        assert primary._record_polling_progress(2) is True  # duplicate progress
    if primary._background_tasks:
        await asyncio.gather(*primary._background_tasks)
    assert _state("primary") == ("delivered", 1)
    assert _state("secondary") == ("failed", 0)
    assert _state("permanent") == ("failed", 0)
    assert _state("already-delivered") == ("delivered", 0)
    assert primary.send.await_count == 1
    assert primary.send.call_args.kwargs["content"].endswith("answer primary")
    assert primary.send.call_args.kwargs["content"].startswith(
        ledger.RECONNECTED_MARKER
    )
    secondary.send.assert_not_awaited()

    with patch.object(secondary, "_write_runtime_status_safe"):
        secondary._record_polling_progress(2)
    if secondary._background_tasks:
        await asyncio.gather(*secondary._background_tasks)
    assert _state("secondary") == ("delivered", 1)
    secondary.send.assert_awaited_once()
    assert primary.send.await_count == 1

    # An obsolete adapter may finish an old poll after its runner registry changes.
    _failed_row("replacement-waiting")
    replacement = _adapter("default")
    runner.adapters[Platform.TELEGRAM] = replacement
    primary._polling_generation = 3
    primary._polling_progress_event = asyncio.Event()
    primary._send_path_degraded = True
    with patch.object(primary, "_write_runtime_status_safe"):
        primary._record_polling_progress(3)
    if primary._background_tasks:
        await asyncio.gather(*primary._background_tasks)
    assert _state("replacement-waiting") == ("failed", 0)
    replacement.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_persisted_after_recovery_sweep_is_redelivered_once():
    primary, secondary = _adapter("default"), _adapter("secondary")
    runner = _runner(primary, secondary)
    primary.gateway_runner = runner
    _failed_row("early")
    with patch.object(primary, "_write_runtime_status_safe"):
        primary._record_polling_progress(2)
    if primary._background_tasks:
        await asyncio.gather(*primary._background_tasks)
    assert _state("early") == ("delivered", 1)

    ledger.record_obligation(
        obligation_id="late",
        session_key="agent:main:telegram:private:42",
        platform="telegram",
        chat_id="42",
        thread_id=None,
        content="late answer",
        adapter_profile="default",
    )
    ledger.mark_attempting("late")
    event = MessageEvent(
        text="question",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM, chat_id="42", chat_type="private"
        ),
        message_id="incoming",
    )
    await primary._finalize_delivery_obligation(
        "late",
        SendResult(success=False, error="send_path_degraded", retryable=True),
        event,
        primary,
    )
    assert _state("late") == ("delivered", 1)
    assert primary.send.await_count == 2
    await runner._redeliver_failed_obligations_for_platform(
        Platform.TELEGRAM, profile="default"
    )
    assert primary.send.await_count == 2
