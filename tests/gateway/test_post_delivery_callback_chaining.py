"""Tests for ``BasePlatformAdapter.register_post_delivery_callback`` chaining.

When two features want to run after the final response lands on the same
session (e.g. background-review release + temporary-progress cleanup), the
registration API chains them rather than clobbering. Per-callback
exceptions are swallowed so one bad callback can't sabotage the others.
Stale-generation registrations are rejected.

The chained wrapper is ``async`` so it transparently supports sync or async
callbacks — the outer invoker in ``_handle_message`` awaits awaitable
callbacks, and a sync wrapper would silently drop coroutine results from
async callbacks chained behind it.
"""
import asyncio
import inspect

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource


class _MinAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="1")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


@pytest.fixture
def adapter():
    return _MinAdapter(PlatformConfig(enabled=True), Platform.TELEGRAM)


def _invoke(cb):
    """Invoke a popped callback, awaiting if it returns a coroutine.

    Single-registration callbacks are returned as the raw user callable
    (sync). Chained callbacks (two or more registrations on the same
    session) are wrapped in an async helper. Tests use this helper so
    they don't have to care which case they're exercising.
    """
    result = cb()
    if inspect.isawaitable(result):
        asyncio.run(result)


class TestPostDeliveryCallbackChaining:
    def test_single_callback_fires(self, adapter):
        fired = []
        adapter.register_post_delivery_callback("s", lambda: fired.append("A"))
        cb = adapter.pop_post_delivery_callback("s")
        _invoke(cb)
        assert fired == ["A"]

    def test_two_callbacks_chain_in_order(self, adapter):
        fired = []
        adapter.register_post_delivery_callback("s", lambda: fired.append("A"))
        adapter.register_post_delivery_callback("s", lambda: fired.append("B"))
        cb = adapter.pop_post_delivery_callback("s")
        _invoke(cb)
        assert fired == ["A", "B"]

    def test_three_callbacks_chain_in_order(self, adapter):
        """Chain composes over an already-chained callback."""
        fired = []
        for label in ("A", "B", "C"):
            adapter.register_post_delivery_callback(
                "s", lambda x=label: fired.append(x)
            )
        cb = adapter.pop_post_delivery_callback("s")
        _invoke(cb)
        assert fired == ["A", "B", "C"]

    def test_newer_generation_does_not_replace_pending_older_generation(
        self, adapter
    ):
        """A queued turn may register before the prior turn's finally block."""
        fired = []
        adapter.register_post_delivery_callback(
            "s", lambda: fired.append("first"), generation=1
        )
        adapter.register_post_delivery_callback(
            "s", lambda: fired.append("second"), generation=2
        )

        first_cb = adapter.pop_post_delivery_callback("s", generation=1)
        second_cb = adapter.pop_post_delivery_callback("s", generation=2)

        assert first_cb is not None
        assert second_cb is not None
        _invoke(first_cb)
        _invoke(second_cb)
        assert fired == ["first", "second"]

    def test_same_generation_still_chains_in_registration_order(self, adapter):
        fired = []
        adapter.register_post_delivery_callback(
            "s", lambda: fired.append("A"), generation=4
        )
        adapter.register_post_delivery_callback(
            "s", lambda: fired.append("B"), generation=4
        )

        callback = adapter.pop_post_delivery_callback("s", generation=4)

        assert callback is not None
        _invoke(callback)
        assert fired == ["A", "B"]

    def test_new_stale_generation_is_rejected(self, adapter):
        adapter.register_post_delivery_callback("s", lambda: None, generation=5)
        adapter.register_post_delivery_callback("s", lambda: None, generation=4)

        assert ("s", 4) not in adapter._post_delivery_callbacks_by_generation
        assert ("s", 5) in adapter._post_delivery_callbacks_by_generation


class TestPostDeliveryCallbackAsyncChaining:
    """When an async callback is chained, the wrapper must await it.

    Regression test for a bug where the sync ``_chained`` wrapper called
    async callbacks without awaiting, silently dropping the returned
    coroutine. This broke ``/goal`` continuations (Discord etc.) where
    the continuation injection is an async ``_deliver()`` coroutine.
    """

    def test_async_callback_in_chain_is_awaited(self, adapter):
        fired = []

        async def async_cb():
            await asyncio.sleep(0)
            fired.append("async")

        adapter.register_post_delivery_callback("s", lambda: fired.append("sync"))
        adapter.register_post_delivery_callback("s", async_cb)
        cb = adapter.pop_post_delivery_callback("s")
        _invoke(cb)
        assert fired == ["sync", "async"]


@pytest.mark.asyncio
async def test_queued_followup_fires_each_turns_generation_callback(adapter):
    """A handoff must not overwrite the guard generation or callback slot.

    ``_process_message_background`` starts an already-queued follow-up before
    the first task enters its post-delivery ``finally`` block. The follow-up
    therefore binds a newer generation on the shared active-session event and
    registers its callback while the first callback is still pending.
    """
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="1234",
        chat_type="private",
        thread_id="77",
    )
    first_event = MessageEvent(
        text="first",
        message_type=MessageType.TEXT,
        source=source,
        message_id="1",
    )
    second_event = MessageEvent(
        text="second",
        message_type=MessageType.TEXT,
        source=source,
        message_id="2",
    )
    session_key = "agent:main:telegram:dm:1234:77"
    adapter._active_sessions[session_key] = asyncio.Event()
    fired = []

    async def _handler(event):
        guard = adapter._active_sessions[session_key]
        if event.text == "first":
            guard._hermes_run_generation = 1
            adapter.register_post_delivery_callback(
                session_key, lambda: fired.append("first"), generation=1
            )
            adapter._pending_messages[session_key] = second_event
            return "first done"

        guard._hermes_run_generation = 2
        adapter.register_post_delivery_callback(
            session_key, lambda: fired.append("second"), generation=2
        )
        return "second done"

    adapter.set_message_handler(_handler)

    await adapter._process_message_background(first_event, session_key)
    for _ in range(100):
        if len(fired) == 2:
            break
        await asyncio.sleep(0.01)

    assert fired == ["first", "second"]
    assert adapter._post_delivery_callbacks == {}
    assert adapter._post_delivery_callbacks_by_generation == {}

