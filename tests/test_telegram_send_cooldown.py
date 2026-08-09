"""TelegramAdapter atomic per-chat Bot API send reservations.

Tests pin concurrency, chunking, independent chats, RetryAfter propagation,
bounded waits, and representative media routing through the shared reservation
primitive.
"""
import asyncio
from datetime import timedelta
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_repo = str(Path(__file__).resolve().parents[1])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    # Prefer the real python-telegram-bot when it is installed. Injecting a
    # MagicMock into sys.modules is process-wide and permanent (module
    # imports are not undone at teardown), so tests that run later in the
    # same session and exercise real PTB objects — e.g.
    # tests/test_telegram_polling_progress_ptb.py — would inherit the mock
    # and fail with "object MagicMock can't be used in 'await' expression".
    # The mock is only a fallback for environments without PTB installed.
    try:
        import telegram  # noqa: F401
        import telegram.constants  # noqa: F401
        import telegram.error  # noqa: F401
        import telegram.ext  # noqa: F401
        return
    except Exception:
        pass

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})
    mod.error.RetryAfter = type(
        "RetryAfter",
        (Exception,),
        {"__init__": lambda self, retry_after=1: setattr(self, "retry_after", retry_after)},
    )
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(
        return_value=MagicMock(message_id=42),
    )
    return adapter


@pytest.mark.asyncio
async def test_first_send_stamps_cooldown_for_same_chat(monkeypatch):
    """A successful send records a cooldown timestamp so the next send to
    the same chat can be gated by ``send()``."""
    adapter = _make_adapter()
    adapter._send_cooldown_seconds = 1.1
    adapter._send_cooldown_max_wait = 5.0

    fake_now = {"t": 1000.0}
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.time.monotonic",
        lambda: fake_now["t"],
    )

    result = await adapter.send("123", "hello")

    assert result.success is True
    # Stamped roughly +1.1s from the start of the call. Allow a small
    # floating-point slack because the gate reads monotonic() multiple
    # times during the request.
    stamped = adapter._send_cooldown_until["123"]
    assert 1000.0 <= stamped <= 1001.5


@pytest.mark.asyncio
async def test_second_send_within_window_blocks(monkeypatch):
    """A second send to the same chat within the cooldown window waits for
    the gate before issuing its Bot API call. We assert via the elapsed
    ``time.monotonic()`` — by advancing a fake clock while the gate sleeps
    we can verify the wait happened without sleeping real time."""
    adapter = _make_adapter()
    adapter._send_cooldown_seconds = 1.1
    adapter._send_cooldown_max_wait = 5.0

    fake_now = {"t": 1000.0}

    def fake_monotonic():
        return fake_now["t"]

    # Capture the real sleep and let it advance our fake clock — this
    # mirrors how the actual gate waits via asyncio.sleep().
    real_sleep = time.sleep

    def fake_sleep(seconds):
        real_sleep(seconds)  # tiny real delay
        fake_now["t"] += seconds

    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.time.monotonic",
        fake_monotonic,
    )
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.asyncio.sleep",
        AsyncMock(side_effect=fake_sleep),
    )

    await adapter.send("123", "first")
    fake_now["t"] = 1000.3  # within cooldown
    await adapter.send("123", "second")

    # Two actual API calls (one per send), and the second one was
    # delayed by the cooldown duration.
    assert adapter._bot.send_message.await_count == 2
    # The cooldown after the second send should reflect the advanced
    # clock — first stamp at ~1001.1, gate waited ~0.8s, second stamp
    # at ~1002.0 (give or take the scheduling jitter).
    stamped = adapter._send_cooldown_until["123"]
    assert stamped >= 1001.5


@pytest.mark.asyncio
async def test_second_send_after_window_passes_immediately(monkeypatch):
    """A second send that arrives after the cooldown has expired must NOT
    wait — only the gate's check is cheap, but we still verify the
    Bot API was called."""
    adapter = _make_adapter()
    adapter._send_cooldown_seconds = 1.1
    adapter._send_cooldown_max_wait = 5.0

    fake_now = {"t": 1000.0}
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.time.monotonic",
        lambda: fake_now["t"],
    )
    sleep_calls = []
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.asyncio.sleep",
        AsyncMock(side_effect=lambda s: sleep_calls.append(s)),
    )

    await adapter.send("123", "first")
    # Jump well past the cooldown window.
    fake_now["t"] = 1050.0
    await adapter.send("123", "second")

    assert adapter._bot.send_message.await_count == 2
    # The gate should NOT have slept — the cooldown was already past.
    assert sleep_calls == []


@pytest.mark.asyncio
async def test_independent_cooldowns_per_chat(monkeypatch):
    """Two different chats have independent cooldowns; a send to chat B
    must not block on chat A's cooldown and vice versa."""
    adapter = _make_adapter()
    adapter._send_cooldown_seconds = 5.0
    adapter._send_cooldown_max_wait = 10.0

    fake_now = {"t": 1000.0}
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.time.monotonic",
        lambda: fake_now["t"],
    )
    sleep_calls = []
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.asyncio.sleep",
        AsyncMock(side_effect=lambda s: sleep_calls.append(s)),
    )

    # First send to chat A stamps A's cooldown.
    await adapter.send("AAA", "to A")
    # Immediate send to chat B should NOT be blocked by A's cooldown.
    await adapter.send("BBB", "to B")

    assert adapter._send_cooldown_until["AAA"] >= 1005.0
    assert adapter._send_cooldown_until["BBB"] >= 1005.0
    # The B-send must not have slept behind A's gate.
    assert sleep_calls == []


@pytest.mark.asyncio
async def test_oversized_wait_returns_retryable_error(monkeypatch):
    """If the gate's wait would exceed ``_send_cooldown_max_wait`` (e.g.
    because Telegram imposed a multi-thousand-second penalty) the gate
    must NOT block the caller indefinitely. Instead it returns a
    retryable error so upstream retries can back off too."""
    adapter = _make_adapter()
    adapter._send_cooldown_seconds = 1.1
    adapter._send_cooldown_max_wait = 5.0

    # Pretend chat A has a 7000-second flood penalty still running.
    adapter._send_cooldown_until["999"] = time.monotonic() + 7000.0

    sleep_calls = []
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.asyncio.sleep",
        AsyncMock(side_effect=lambda s: sleep_calls.append(s)),
    )

    result = await adapter.send("999", "hello")

    # Gate must NOT have slept — the wait is too long.
    assert sleep_calls == []
    # And must have returned a retryable error tagged with the wait time
    # so the caller can decide what to do.
    assert result.success is False
    assert result.retryable is True
    assert "flood_control" in (result.error or "")
    # No Bot API call should have been made.
    assert adapter._bot.send_message.await_count == 0


@pytest.mark.asyncio
async def test_concurrent_rich_sends_reserve_distinct_slots(monkeypatch):
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="***", extra={"rich_messages": True})
    )
    sleeps: list[float] = []
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    class RichBot:
        calls = 0

        async def do_api_request(self, endpoint, api_kwargs):
            assert endpoint == "sendRichMessage"
            self.calls += 1
            if self.calls == 1:
                first_entered.set()
                await release_first.wait()
            return SimpleNamespace(message_id=self.calls)

    adapter._bot = RichBot()
    adapter._send_cooldown_seconds = 0.25
    adapter._send_cooldown_max_wait = 1.0

    async def fake_sleep(delay):
        sleeps.append(delay)
        await original_sleep(0)

    original_sleep = asyncio.sleep
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.asyncio.sleep",
        fake_sleep,
    )
    content = "| A | B |\n|---|---|\n| 1 | 2 |"
    first = asyncio.create_task(adapter.send("42", content, metadata={"notify": True}))
    await first_entered.wait()
    second = asyncio.create_task(adapter.send("42", content, metadata={"notify": True}))
    await original_sleep(0)
    release_first.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.success and second_result.success
    assert adapter._bot.calls == 2
    assert len(sleeps) == 1
    assert sleeps[0] > 0


@pytest.mark.asyncio
async def test_each_message_chunk_reserves_its_own_slot(monkeypatch):
    adapter = _make_adapter()
    adapter._send_cooldown_seconds = 0.2
    adapter._send_cooldown_max_wait = 5.0
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.asyncio.sleep",
        fake_sleep,
    )
    content = "x" * (adapter.MAX_MESSAGE_LENGTH + 200)
    result = await adapter.send("chunk-chat", content)

    assert result.success
    assert adapter._bot.send_message.await_count == 2
    assert len(sleeps) == 1
    assert sleeps[0] > 0


@pytest.mark.asyncio
async def test_retry_after_is_shared_with_already_waiting_sender():
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="***", extra={"rich_messages": True})
    )

    class Flooded(Exception):
        retry_after = 7.0

    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    class FloodBot:
        calls = 0

        async def do_api_request(self, endpoint, api_kwargs):
            self.calls += 1
            first_entered.set()
            await release_first.wait()
            raise Flooded("Retry after 7")

    adapter._bot = FloodBot()
    adapter._send_cooldown_seconds = 0.1
    adapter._send_cooldown_max_wait = 5.0
    content = "| A | B |\n|---|---|\n| 1 | 2 |"

    first_task = asyncio.create_task(
        adapter.send("flood-chat", content, metadata={"notify": True})
    )
    await first_entered.wait()
    second_task = asyncio.create_task(
        adapter.send("flood-chat", content, metadata={"notify": True})
    )
    await asyncio.sleep(0)
    release_first.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first.success is False
    assert first.retry_after == 5.0
    assert second.success is False
    assert second.retryable is True
    assert second.retry_after == 5.0
    assert adapter._bot.calls == 1


@pytest.mark.asyncio
async def test_native_media_helper_uses_same_atomic_reservation(monkeypatch):
    adapter = _make_adapter()
    adapter._send_cooldown_seconds = 0.2
    adapter._send_cooldown_max_wait = 1.0
    sleeps: list[float] = []
    send_fn = AsyncMock(return_value=SimpleNamespace(message_id=1))

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.asyncio.sleep",
        fake_sleep,
    )
    kwargs = {"chat_id": 91, "photo": b"image"}
    await adapter._send_with_dm_topic_reply_anchor_retry(
        send_fn, kwargs, None, None, "photo"
    )
    await adapter._send_with_dm_topic_reply_anchor_retry(
        send_fn, kwargs, None, None, "photo"
    )

    assert send_fn.await_count == 2
    assert len(sleeps) == 1
    assert sleeps[0] > 0


@pytest.mark.asyncio
async def test_native_media_retry_after_is_retryable_not_fallback():
    adapter = _make_adapter()

    class Flooded(Exception):
        retry_after = 4.0

    send_fn = AsyncMock(side_effect=Flooded("Retry after 4"))
    with pytest.raises(Exception) as raised:
        await adapter._send_with_dm_topic_reply_anchor_retry(
            send_fn,
            {"chat_id": 91, "photo": b"image"},
            None,
            None,
            "photo",
        )

    assert raised.value.__class__.__name__ == "_TelegramSendCooldownExceeded"
    assert getattr(raised.value, "retry_after", None) == 4.0
    assert send_fn.await_count == 1


@pytest.mark.asyncio
async def test_cancelled_send_releases_chat_lock():
    adapter = _make_adapter()
    adapter._send_cooldown_seconds = 0.0
    adapter._send_cooldown_max_wait = 5.0
    started = asyncio.Event()

    async def blocked_send():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        adapter._run_send_call("cancel-chat", blocked_send)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert adapter._send_cooldown_locks["cancel-chat"].locked() is False
    result = await adapter._run_send_call(
        "cancel-chat",
        AsyncMock(return_value="ok"),
    )
    assert result == "ok"


@pytest.mark.asyncio
async def test_extreme_server_retry_after_never_sleeps_inline(monkeypatch):
    adapter = _make_adapter()
    adapter._send_cooldown_max_wait = 5.0
    sleeps: list[float] = []

    class Flooded(Exception):
        retry_after = 7000.0

    bot = adapter._bot
    assert bot is not None
    bot.send_message.side_effect = Flooded("Retry after 7000")

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.asyncio.sleep",
        fake_sleep,
    )
    result = await adapter.send("flood-chat", "hello", metadata={"notify": True})

    assert result.success is False
    assert result.retryable is True
    assert result.retry_after == 5.0
    assert sleeps == []
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_idle_cooldown_state_map_is_bounded():
    adapter = _make_adapter()
    adapter._send_cooldown_users = {}
    adapter._send_cooldown_state_max = 2
    adapter._send_cooldown_seconds = 0.0

    for chat_id in ("one", "two", "three"):
        await adapter._run_send_call(chat_id, AsyncMock(return_value="ok"))

    assert len(adapter._send_cooldown_locks) <= 2
    assert len(adapter._send_cooldown_until) <= 2
    assert adapter._send_cooldown_users == {}


def test_retry_after_accepts_ptb_timedelta_mode():
    error = Exception("flood control")
    error.retry_after = timedelta(seconds=7)  # type: ignore[attr-defined]

    assert TelegramAdapter._telegram_retry_after(error) == 7.0
