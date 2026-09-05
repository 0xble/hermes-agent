"""Internal polling recovery must wake the real, identity-scoped delivery ledger."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway import delivery_ledger as dl
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from plugins.platforms.telegram.adapter import TelegramAdapter, _POLLING_PROGRESS_TIMEOUT
from tests.gateway.test_telegram_polling_health_confirmation import _bare_adapter


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "_db_path", lambda: tmp_path / "state.db")
    adapter = _bare_adapter()
    adapter._owner_profile = "default"
    adapter._background_tasks = set()
    adapter._bot = object()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="sent"))
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {}
    runner._active_profile_name = lambda: "default"
    runner.session_store = MagicMock()
    runner._async_session_store = MagicMock()
    runner._async_session_store._store = runner.session_store
    runner._async_session_store.clear_resume_pending_for_obligation = AsyncMock(return_value=True)
    setattr(adapter, "gateway_runner", runner)
    return adapter, runner


def record(oid="answer", profile="default", error="send_path_degraded"):
    dl.record_obligation(
        obligation_id=oid, session_key="agent:main:telegram:dm:1", platform="telegram",
        chat_id="1", thread_id="2", content="the preserved answer",
        adapter_profile=profile, obligation_kind="agent_final", turn_token="turn-1",
    )
    dl.mark_failed(oid, error)


def state(oid="answer"):
    with dl._connect() as conn:
        return conn.execute(
            "SELECT state, attempts FROM delivery_obligations WHERE obligation_id=?", (oid,)
        ).fetchone()


async def settle(adapter):
    if adapter._background_tasks:
        await asyncio.gather(*tuple(adapter._background_tasks))


@pytest.mark.asyncio
async def test_degraded_retry_waits_for_polling_health_window(setup):
    adapter, _ = setup
    result = await TelegramAdapter.send(adapter, "1", "answer")
    assert not result.success
    assert result.retry_after == _POLLING_PROGRESS_TIMEOUT
    assert result.error_kind == "transient"


@pytest.mark.asyncio
async def test_same_adapter_recovers_and_delivers_once(setup):
    adapter, runner = setup
    record()
    adapter._record_polling_progress(1)
    adapter._record_polling_progress(1)
    await settle(adapter)
    assert state() == ("delivered", 1)
    adapter.send.assert_awaited_once()
    assert adapter.send.call_args.kwargs["metadata"]["thread_id"] == "2"
    runner._async_session_store.clear_resume_pending_for_obligation.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_before_failed_row_is_recorded(setup):
    adapter, _ = setup
    adapter._record_polling_progress(1)
    await settle(adapter)
    record()
    # Producer's late-failure signal after the initial recovery sweep was empty.
    await adapter._redeliver_recovered_send_path()
    assert state() == ("delivered", 1)
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_exact_profile_and_transient_only(setup):
    adapter, runner = setup
    adapter._owner_profile = "owner"
    runner._profile_adapters = {"owner": {Platform.TELEGRAM: adapter}}
    runner.adapters = {}
    record(profile="owner")
    record("other", profile="default")
    record("permanent", profile="owner", error="Forbidden")
    record("ambiguous", profile="owner", error="ReadTimeout")
    adapter._record_polling_progress(1)
    await settle(adapter)
    assert state() == ("delivered", 1)
    for oid in ("other", "permanent", "ambiguous"):
        assert state(oid) == ("failed", 0)
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["stale_generation", "replaced", "teardown", "degraded_again"])
async def test_invalid_recovery_cannot_claim_rows(setup, blocked):
    adapter, runner = setup
    record()
    if blocked == "replaced":
        runner.adapters[Platform.TELEGRAM] = object()
    if blocked == "teardown":
        adapter._polling_teardown_started = True
    adapter._record_polling_progress(0 if blocked == "stale_generation" else 1)
    if blocked == "degraded_again":
        adapter._send_path_degraded = True
    await settle(adapter)
    assert state() == ("failed", 0)
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_late_failure_after_recovery_is_not_stranded(setup):
    adapter, _ = setup
    record()
    calls = 0

    async def send(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            # Recovery wins the race with persistence of this replay's failure.
            adapter._send_path_degraded = True
            adapter._record_polling_progress(1)
            await settle(adapter)
            return SendResult(success=False, error="send_path_degraded", retryable=True)
        return SendResult(success=True, message_id="sent")

    adapter.send.side_effect = send
    adapter._send_path_degraded = False
    await adapter._redeliver_recovered_send_path()
    assert state() == ("delivered", 2)
    assert calls == 2


@pytest.mark.asyncio
async def test_repeated_late_failures_obey_existing_attempt_cap(setup):
    adapter, _ = setup
    record()
    adapter._send_path_degraded = False
    adapter.send.return_value = SendResult(success=False, error="send_path_degraded", retryable=True)
    await adapter._redeliver_recovered_send_path()
    assert state() == ("abandoned", dl.MAX_ATTEMPTS)
    assert adapter.send.await_count == dl.MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_concurrent_signals_claim_once(setup):
    adapter, _ = setup
    record()
    adapter._send_path_degraded = False
    await asyncio.gather(*(adapter._redeliver_recovered_send_path() for _ in range(4)))
    assert state() == ("delivered", 1)
    adapter.send.assert_awaited_once()
