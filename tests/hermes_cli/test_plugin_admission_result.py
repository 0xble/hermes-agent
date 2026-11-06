"""The public opt-in must survive manager/context forwarding without bool coercion."""
import asyncio
import concurrent.futures
import threading
from unittest.mock import MagicMock

import pytest

from hermes_cli.plugin_injection import GatewayInjectionHandle, InjectionDelivery, immediate_result
from tests.hermes_cli.test_plugin_message_injection import _context, _write_plugin_config


@pytest.mark.parametrize("accepted,reason", [(True, "adopted"), (False, "queue_full")])
def test_context_preserves_handle_and_correlation(tmp_path, monkeypatch, accepted, reason):
    _write_plugin_config(tmp_path, monkeypatch, {"allow_gateway_injection": True})
    ctx, manager = _context()
    handle = immediate_result(accepted, reason, session_key="key", correlation_id="relay-1")
    injector = MagicMock(return_value=handle)
    manager.set_gateway_message_injector(object(), injector)
    assert ctx.inject_message("note", session_key="key", await_dispatch=True, correlation_id="relay-1") is handle
    result = handle.result(0)
    assert result.accepted is accepted and result.reason == reason and result.correlation_id == "relay-1"
    assert result.settled and handle.settled
    assert injector.call_args.kwargs["await_dispatch"] is True
    assert injector.call_args.kwargs["correlation_id"] == "relay-1"
    with pytest.raises(TypeError):
        bool(handle)


@pytest.mark.asyncio
async def test_loop_blocking_is_rejected_and_cancelled_observer_does_not_cancel_dispatch():
    loop = asyncio.get_running_loop()
    release = asyncio.Event()
    delivery = InjectionDelivery("key", "relay-1")
    async def dispatch():
        delivery.started = True
        await release.wait()
        delivery.finish(True, "adopted", admission_id="admission-1")
        return True
    task = loop.create_task(delivery.observe(dispatch()))
    handle = GatewayInjectionHandle.track(task, delivery, loop)
    with pytest.raises(RuntimeError, match="would block"):
        handle.result(0)
    waiter = asyncio.create_task(handle.wait())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    result = await handle.wait(timeout=0)
    assert result.indeterminate and not result.safe_to_retry and not task.cancelled()
    release.set()
    result = await handle
    assert result.accepted and result.admission_id == "admission-1"


@pytest.mark.asyncio
async def test_cancelling_consumer_propagates_while_dispatch_continues():
    loop = asyncio.get_running_loop()
    release, observing = asyncio.Event(), asyncio.Event()
    delivery = InjectionDelivery("key", "relay-cancel")

    async def dispatch():
        delivery.started = True
        await release.wait()
        delivery.finish(True, "adopted", admission_id="admission-after-cancel")
        return True

    task = loop.create_task(delivery.observe(dispatch()))
    handle = GatewayInjectionHandle.track(task, delivery, loop)

    async def consumer():
        observing.set()
        return await handle

    consumer_task = loop.create_task(consumer())
    await observing.wait()
    consumer_task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await consumer_task
        assert consumer_task.cancelled()
        assert not task.done() and not handle.settled
        observed = await handle.wait(timeout=0)
        assert observed.indeterminate and not observed.refused and not observed.safe_to_retry
    finally:
        release.set()
        await task
    result = await handle
    assert result.accepted and result.admission_id == "admission-after-cancel"


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper_starts", [False, True])
async def test_worker_cancellation_closes_unobserved_coroutine_on_owner_loop(wrapper_starts):
    loop = asyncio.get_running_loop()
    owner_thread = threading.get_ident()
    closed_on = []
    delivery = InjectionDelivery("key", "relay-worker")
    dispatch = concurrent.futures.Future()
    coro = MagicMock()
    coro.close.side_effect = lambda: closed_on.append(threading.get_ident())
    handle = GatewayInjectionHandle.track(dispatch, delivery, loop, coro)

    def cancel_from_worker():
        if wrapper_starts:
            loop.call_soon_threadsafe(setattr, delivery, "observing", True)
        return handle.cancel()

    assert await asyncio.to_thread(cancel_from_worker)
    result = await handle
    assert result.indeterminate and not result.safe_to_retry
    # Receipt settlement does not wait for the separately queued cleanup.
    cleanup_turn = asyncio.Event()
    loop.call_soon(cleanup_turn.set)
    await cleanup_turn.wait()
    assert closed_on == ([] if wrapper_starts else [owner_thread])


def test_closed_loop_cancellation_still_settles_handle():
    loop = asyncio.new_event_loop()
    loop.close()
    dispatch, coro = concurrent.futures.Future(), MagicMock()
    handle = GatewayInjectionHandle.track(dispatch, InjectionDelivery("key"), loop, coro)
    assert handle.cancel()
    result = handle.result(0)
    assert result.indeterminate and not result.safe_to_retry
    coro.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_commit_receipt_wins_when_cancellation_callback_runs_after_commit():
    delivery = InjectionDelivery("key", "relay-1")
    delivery.started = True
    delivery.finish(True, "adopted", admission_id="admission-1")
    dispatch = concurrent.futures.Future()
    handle = GatewayInjectionHandle.track(dispatch, delivery, asyncio.get_running_loop())
    dispatch.cancel()
    result = await handle
    assert result.accepted and not result.safe_to_retry and result.admission_id == "admission-1"
