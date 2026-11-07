"""Retire-now interrupts a turn without taking ownership of its queued obligations."""
import asyncio

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource, build_session_key
from tests.gateway.restart_test_helpers import RestartTestAdapter


def test_cancel_preserving_pending_drains_retained_wake():
    asyncio.run(_cancel_preserving_pending_drains_retained_wake())


async def _cancel_preserving_pending_drains_retained_wake():
    adapter = RestartTestAdapter()
    source = SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="c1",
                           chat_type="dm")
    key = build_session_key(source)
    entered, never = asyncio.Event(), asyncio.Event()
    seen = []

    async def handler(event):
        seen.append(event.text)
        if event.text == "first":
            entered.set()
            await never.wait()
        return "processed: " + event.text

    adapter.set_message_handler(handler)
    first = MessageEvent(text="first", source=source, message_id="first")
    wake = MessageEvent(text="wake", source=source, message_id="wake", internal=True)
    try:
        await adapter.handle_message(first)
        await asyncio.wait_for(entered.wait(), 5)
        old_owner = adapter._session_tasks[key]
        await adapter.handle_message(wake)
        assert adapter._pending_messages[key] is wake
        # The exact preserve-mode call made by telegram_retire.request(now=True).
        await adapter.cancel_session_processing(key, discard_pending=False)
        assert old_owner.cancelled()
        await asyncio.wait_for(asyncio.gather(*adapter._background_tasks, return_exceptions=True), 5)
        assert seen == ["first", "wake"], seen
        assert key not in adapter._pending_messages
    finally:
        await adapter.cancel_background_tasks()
