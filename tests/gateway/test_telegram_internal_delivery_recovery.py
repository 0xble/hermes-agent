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
        ledger.RECOVERED_MARKER
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


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["default", "secondary"])
@pytest.mark.parametrize("cancel_count", [1, 2])
@pytest.mark.parametrize("phase", ["claim", "clear", "note", "send", "delivered_write", "refund_before", "refund_after"])
async def test_cancelled_recovery_settles_or_refunds_each_claim_once(
    monkeypatch, profile, cancel_count, phase,
):
    import threading

    primary, secondary = _adapter("default"), _adapter("secondary")
    runner = _runner(primary, secondary)
    adapter, sibling = (primary, secondary) if profile == "default" else (secondary, primary)
    _failed_row("first", profile)
    _failed_row("second", profile)
    _failed_row("sibling", "secondary" if profile == "default" else "default")
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), asyncio.Event()
    thread_release = threading.Event()
    writes = []

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return SendResult(success=True, message_id="sent") if phase == "send" else None

    def delay_claim():
        real_sweep = ledger.sweep_failed_for_runtime
        def delayed_sweep(*args, **kwargs):
            rows = real_sweep(*args, **kwargs)
            loop.call_soon_threadsafe(entered.set)
            assert thread_release.wait(10)
            return rows
        monkeypatch.setattr(ledger, "sweep_failed_for_runtime", delayed_sweep)
    def delay_write():
        name = "mark_delivered" if phase == "delivered_write" else "release_runtime_claim"
        real_write = getattr(ledger, name)
        def delayed_write(*args, **kwargs):
            writes.append(args[0])
            if len(writes) != 1:
                return real_write(*args, **kwargs)
            result = real_write(*args, **kwargs) if phase == "refund_after" else None
            loop.call_soon_threadsafe(entered.set)
            assert thread_release.wait(10)
            return result if phase == "refund_after" else real_write(*args, **kwargs)
        monkeypatch.setattr(ledger, name, delayed_write)
        if phase.startswith("refund"):
            runner._async_session_store.clear_resume_pending.side_effect = RuntimeError("store unavailable")
    def delay_clear():
        runner._async_session_store.clear_resume_pending.side_effect = blocked

    def delay_note():
        runner._redelivery_restart_note_event = blocked

    def delay_send():
        adapter.send.side_effect = blocked

    {
        "claim": delay_claim,
        "delivered_write": delay_write,
        "refund_before": delay_write,
        "refund_after": delay_write,
        "clear": delay_clear,
        "note": delay_note,
        "send": delay_send,
    }[phase]()

    task = asyncio.create_task(
        runner._redeliver_failed_obligations_for_platform(Platform.TELEGRAM, profile=profile)
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert _state("first")[0] == ("failed" if phase == "refund_after" else "attempting")
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
        if phase == "refund_after":
            # A replacement can claim the row before the older refund await returns.
            # Repeated cancellation must not restart that refund against this claim.
            successor = ledger.sweep_failed_for_runtime("telegram", profile=profile)
            assert [row["obligation_id"] for row in successor] == ["first"]
        thread_release.set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        if phase in {"send", "delivered_write"}:
            assert _state("first") == ("delivered", 1)
            assert adapter.send.await_count == 1
        elif phase == "refund_after":
            assert _state("first") == ("attempting", 1)
            assert writes.count("first") == 1
            adapter.send.assert_not_awaited()
        else:
            assert _state("first") == ("failed", 0)
            adapter.send.assert_not_awaited()
        assert _state("second") == _state("sibling") == ("failed", 0)
        sibling.send.assert_not_awaited()
    finally:
        thread_release.set()
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["default", "secondary"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_sweep_failure_preserves_pending_teardown_cancellation(monkeypatch, profile, cancelled):
    import threading

    primary, secondary = _adapter("default"), _adapter("secondary")
    runner = _runner(primary, secondary)
    _failed_row("waiting", profile)
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()

    def failing_sweep(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        raise RuntimeError("ledger sweep unavailable")

    monkeypatch.setattr(ledger, "sweep_failed_for_runtime", failing_sweep)
    task = asyncio.create_task(
        runner._redeliver_failed_obligations_for_platform(Platform.TELEGRAM, profile=profile)
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        if cancelled:
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
        release.set()
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
        else:
            assert await asyncio.wait_for(task, 10) == 0
        assert _state("waiting") == ("failed", 0)
        primary.send.assert_not_awaited()
        secondary.send.assert_not_awaited()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
