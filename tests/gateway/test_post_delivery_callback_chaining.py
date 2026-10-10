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
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
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


def test_queued_generation_preserves_both_callbacks(adapter):
    fired = []
    adapter.register_post_delivery_callback("s", lambda: fired.append("first"), generation=1)
    adapter.register_post_delivery_callback("s", lambda: fired.append("second"), generation=2)
    first = adapter.pop_post_delivery_callback("s", generation=1)
    second = adapter.pop_post_delivery_callback("s", generation=2)
    assert first is not None and second is not None
    _invoke(first)
    _invoke(second)
    assert fired == ["first", "second"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prebound", [True, False])
async def test_cancelled_predecessor_does_not_fire_successor_callback(adapter, prebound):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1234", chat_type="private")
    event = MessageEvent(text="first", message_type=MessageType.TEXT, source=source, message_id="1")
    key = "agent:main:telegram:dm:1234"
    guard = asyncio.Event()
    if prebound:
        setattr(guard, "_hermes_run_generation", 1)
    adapter._active_sessions[key] = guard
    started = asyncio.Event()
    fired = []

    async def handler(_event):
        setattr(guard, "_hermes_run_generation", 1)
        started.set()
        await asyncio.Event().wait()

    adapter.set_message_handler(handler)
    task = asyncio.create_task(adapter._process_message_background(event, key))
    await asyncio.wait_for(started.wait(), timeout=2)
    adapter.register_post_delivery_callback(key, lambda: fired.append("successor"), generation=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert fired == []
    successor = adapter.pop_post_delivery_callback(key, generation=2)
    assert successor is not None


@pytest.mark.asyncio
async def test_failed_predecessor_fires_own_callback(adapter):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1234", chat_type="private")
    event = MessageEvent(text="first", message_type=MessageType.TEXT, source=source, message_id="1")
    key = "agent:main:telegram:dm:1234"
    guard = asyncio.Event()
    setattr(guard, "_hermes_run_generation", 1)
    adapter._active_sessions[key] = guard
    fired = []

    async def handler(_event):
        adapter.register_post_delivery_callback(key, lambda: fired.append("predecessor"), generation=1)
        raise RuntimeError("handler failed")

    adapter.set_message_handler(handler)
    await adapter._process_message_background(event, key)

    assert fired == ["predecessor"]


@pytest.mark.asyncio
async def test_queued_handoff_fires_each_generation(adapter):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1234", chat_type="private")
    first = MessageEvent(text="first", message_type=MessageType.TEXT, source=source, message_id="1")
    second = MessageEvent(text="second", message_type=MessageType.TEXT, source=source, message_id="2")
    key = "agent:main:telegram:dm:1234"
    adapter._active_sessions[key] = asyncio.Event()
    fired = []

    async def handler(event):
        guard = adapter._active_sessions[key]
        generation = 1 if event.text == "first" else 2
        setattr(guard, "_hermes_run_generation", generation)
        adapter.register_post_delivery_callback(
            key, lambda: fired.append(event.text), generation=generation,
        )
        if generation == 1:
            adapter._pending_messages[key] = second
        return f"{event.text} done"

    adapter.set_message_handler(handler)
    await adapter._process_message_background(first, key)
    for _ in range(100):
        if len(fired) == 2:
            break
        await asyncio.sleep(0.01)
    assert fired == ["first", "second"]
    assert adapter._post_delivery_callbacks == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
async def test_failed_or_cancelled_drained_followup_fires_own_callback(adapter, outcome):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1234", chat_type="private")
    first = MessageEvent(text="first", message_type=MessageType.TEXT, source=source, message_id="1")
    second = MessageEvent(text="second", message_type=MessageType.TEXT, source=source, message_id="2")
    key = "agent:main:telegram:dm:1234"
    guard = asyncio.Event()
    adapter._active_sessions[key] = guard
    started = asyncio.Event()
    release = asyncio.Event()
    fired = []

    async def handler(event):
        generation = 1 if event.text == "first" else 2
        setattr(guard, "_hermes_run_generation", generation)
        adapter.register_post_delivery_callback(
            key, lambda: fired.append(event.text), generation=generation,
        )
        if generation == 1:
            adapter._pending_messages[key] = second
            return "first done"
        started.set()
        await release.wait()
        raise RuntimeError("follow-up failed")

    adapter.set_message_handler(handler)
    await adapter._process_message_background(first, key)
    await asyncio.wait_for(started.wait(), timeout=2)
    task = adapter._session_tasks[key]
    if outcome == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        release.set()
        await asyncio.wait_for(task, timeout=2)

    assert fired == ["first", "second"]
    assert adapter._post_delivery_callbacks == {}
    assert adapter._post_delivery_callbacks_by_generation == {}


@pytest.mark.asyncio
async def test_unbound_in_turn_bare_callback_fires_once(adapter):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1234", chat_type="private")
    event = MessageEvent(text="first", message_type=MessageType.TEXT, source=source, message_id="1")
    key = "agent:main:telegram:dm:1234"
    guard = asyncio.Event()
    adapter._active_sessions[key] = guard
    fired = []

    async def handler(_event):
        adapter.register_post_delivery_callback(key, lambda: fired.append("callback"))
        return None

    adapter.set_message_handler(handler)
    await adapter._process_message_background(event, key)
    assert fired == ["callback"]
    assert adapter._post_delivery_callbacks == {}

    async def no_callback(_event):
        return None

    adapter.set_message_handler(no_callback)
    adapter._active_sessions[key] = asyncio.Event()
    await adapter._process_message_background(event, key)

    assert fired == ["callback"]
    assert adapter._post_delivery_callbacks == {}
    assert adapter._post_delivery_callbacks_by_generation == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["unbound", "prebound", "handler"])
@pytest.mark.parametrize("registration", ["direct", "api"])
async def test_preregistered_legacy_callback_fires_once_after_send(
    adapter, binding, registration,
):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1234", chat_type="private")
    event = MessageEvent(text="first", message_type=MessageType.TEXT, source=source, message_id="1")
    key = "agent:main:telegram:dm:1234"
    guard = asyncio.Event()
    if binding == "prebound":
        setattr(guard, "_hermes_run_generation", 1)
    adapter._active_sessions[key] = guard
    fired = []

    async def legacy_callback():
        fired.append("legacy")

    if registration == "direct":
        adapter._post_delivery_callbacks[key] = legacy_callback
    else:
        adapter.register_post_delivery_callback(key, legacy_callback)

    async def handler(_event):
        if binding == "handler":
            setattr(guard, "_hermes_run_generation", 1)
        if binding != "unbound":
            adapter.register_post_delivery_callback(
                key, lambda: fired.append("owned"), generation=1,
            )
        return "done"

    async def send(*args, **kwargs):
        fired.append("send")
        return SendResult(success=True, message_id="1")

    adapter.send = send
    adapter.set_message_handler(handler)
    await adapter._process_message_background(event, key)

    assert fired == ["send", "legacy"] + (["owned"] if binding != "unbound" else [])
    assert adapter._post_delivery_callbacks == {}
    assert adapter._post_delivery_callbacks_by_generation == {}
    await adapter._process_message_background(event, key)
    assert fired.count("legacy") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["cancelled", "failed"])
@pytest.mark.parametrize("registration", ["preregistered", "in_turn"])
async def test_predecessor_claims_legacy_callback_without_consuming_successor(
    adapter, outcome, registration,
):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1234", chat_type="private")
    event = MessageEvent(text="first", message_type=MessageType.TEXT, source=source, message_id="1")
    key = "agent:main:telegram:dm:1234"
    guard = asyncio.Event()
    adapter._active_sessions[key] = guard
    started, release = asyncio.Event(), asyncio.Event()
    fired = []
    if registration == "preregistered":
        adapter.register_post_delivery_callback(key, lambda: fired.append("predecessor"))

    async def handler(_event):
        setattr(guard, "_hermes_run_generation", 1)
        if registration == "in_turn":
            adapter.register_post_delivery_callback(key, lambda: fired.append("predecessor"))
        started.set()
        await release.wait()
        raise RuntimeError("handler failed")

    adapter.set_message_handler(handler)
    task = asyncio.create_task(adapter._process_message_background(event, key))
    await asyncio.wait_for(started.wait(), timeout=2)
    successor = lambda: fired.append("successor")
    adapter.register_post_delivery_callback(key, successor)
    if outcome == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        release.set()
        await asyncio.wait_for(task, timeout=2)

    assert fired == ["predecessor"]
    assert adapter.pop_post_delivery_callback(key) is successor


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

