"""The Telegram update consumer never waits on outbound pacing, and a busy consumer is not "wedged".

On 2026-10-05, Telegram polling was force-restarted at 12:13 and 13:26 PDT. Both times the two
"stuck" probes, 90s apart, had each caught a *different* update mid-handler (updates 395 and 398,
then 515 and 519), with other updates dispatched in between. Telegram counts the batch being
handled as pending until the next getUpdates confirms its offset, and the controlled poller does
not poll again until that batch drains. Update 519 was a ``/queue`` sent to a busy session. Its
inline reply waited on the chat's outbound budget inside the serial consumer for more than 20s.
Each restart then lost about 30s, because the reconnect refused while the old in-process poller
was still releasing the token.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import (
    SendResult, in_ingress_consumer, ingress_consumer_scope, leave_ingress_consumer)
from plugins.platforms.telegram.adapter import TelegramAdapter


def _probe_adapter(pending: int) -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._webhook_mode = False
    adapter._app = MagicMock()
    adapter._app.updater.running = True
    bot = MagicMock()
    bot.get_webhook_info = AsyncMock(return_value=MagicMock(pending_update_count=pending))
    adapter._app.bot = adapter._bot = bot
    return adapter


@pytest.mark.asyncio
async def test_pending_backlog_with_dispatch_progress_is_not_a_wedge():
    """The 13:26 shape: pending=1 at both probes, but updates were dispatched in between."""
    adapter = _probe_adapter(pending=1)
    with patch.object(adapter, "_handle_polling_network_error", new=AsyncMock()) as recovery:
        for _ in range(5):
            await adapter._probe_pending_updates(adapter._bot, 5)
            adapter._updates_dispatched_total += 4  # the consumer kept handling updates
        assert adapter._polling_error_task is None
    recovery.assert_not_called()


@pytest.mark.asyncio
async def test_pending_backlog_without_dispatch_progress_still_escalates():
    adapter = _probe_adapter(pending=1)
    recovery = AsyncMock()
    with patch.object(adapter, "_handle_polling_network_error", new=recovery):
        await adapter._probe_pending_updates(adapter._bot, 5)
        adapter._updates_dispatched_total += 2
        await adapter._probe_pending_updates(adapter._bot, 5)  # progress: a new window starts here
        assert adapter._polling_error_task is None
        await adapter._probe_pending_updates(adapter._bot, 5)  # nothing dispatched since: wedged
        await adapter._polling_error_task
    recovery.assert_awaited_once()


async def _on_consumer(coro):
    token = ingress_consumer_scope()
    try:
        return await coro
    finally:
        leave_ingress_consumer(token)


def _inline_adapter(send_gate: asyncio.Event, sends: list):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))

    async def handler(_event):
        return "Queued for the next turn."

    async def paced_send(**kwargs):
        await send_gate.wait()  # the chat's outbound budget slot
        sends.append(kwargs["content"])
        return SendResult(success=True, message_id="1")

    adapter._message_handler = handler
    adapter._send_with_retry = paced_send
    return adapter


def _event():
    return SimpleNamespace(source=SimpleNamespace(chat_id="2027045491", thread_id=None), metadata={},
                           message_id="519", reply_to_message_id=None)


@pytest.mark.asyncio
async def test_command_reply_on_the_consumer_does_not_wait_for_the_budget():
    gate, sends = asyncio.Event(), []
    adapter = _inline_adapter(gate, sends)
    await asyncio.wait_for(_on_consumer(adapter._dispatch_inline_reply(_event())), timeout=1)
    assert sends == []  # the consumer returned while the reply waits for its slot
    gate.set()
    await asyncio.gather(*adapter._background_tasks)
    assert sends == ["Queued for the next turn."]


@pytest.mark.asyncio
async def test_command_reply_off_the_consumer_is_still_awaited():
    gate, sends = asyncio.Event(), []
    gate.set()
    adapter = _inline_adapter(gate, sends)
    await adapter._dispatch_inline_reply(_event())
    assert sends == ["Queued for the next turn."]


@pytest.mark.asyncio
async def test_busy_ack_on_the_consumer_does_not_wait_for_the_budget():
    from gateway.run import GatewayRunner

    gate, sends = asyncio.Event(), []
    adapter = _inline_adapter(gate, sends)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._reply_anchor_for_event = lambda event: None
    runner._busy_reply_to = lambda event, anchor: None
    runner._thread_metadata_for_source = lambda source, anchor: {}
    await asyncio.wait_for(_on_consumer(runner._send_busy_reply(_event(), adapter, "busy")), timeout=1)
    assert sends == []
    gate.set()
    await asyncio.gather(*adapter._background_tasks)
    assert sends == ["busy"]


@pytest.mark.asyncio
async def test_consumer_role_is_not_inherited_by_spawned_tasks():
    async def check():
        assert in_ingress_consumer()
        child = asyncio.get_running_loop().create_task(asyncio.sleep(0, result=None))
        await child

        async def inner():
            return in_ingress_consumer()

        assert await asyncio.get_running_loop().create_task(inner()) is False

    await _on_consumer(check())
    assert not in_ingress_consumer()


@pytest.mark.asyncio
async def test_reconnect_waits_for_the_old_poller_to_release_the_token():
    from plugins.platforms.telegram import polling_transfer

    async def stopping():
        await asyncio.sleep(0.05)

    task = asyncio.get_running_loop().create_task(stopping())
    polling_transfer._active_pollers["tok"] = task
    task.add_done_callback(lambda t: asyncio.get_running_loop().call_soon(
        polling_transfer._active_pollers.pop, "tok", None))
    try:
        assert await polling_transfer.wait_for_poller_release("tok", 5.0)
    finally:
        polling_transfer._active_pollers.pop("tok", None)


@pytest.mark.asyncio
async def test_a_poller_that_never_stops_still_refuses_the_token():
    from plugins.platforms.telegram import polling_transfer

    task = asyncio.get_running_loop().create_task(asyncio.sleep(10))
    polling_transfer._active_pollers["tok2"] = task
    try:
        assert not await polling_transfer.wait_for_poller_release("tok2", 0.05)
    finally:
        task.cancel()
        polling_transfer._active_pollers.pop("tok2", None)
