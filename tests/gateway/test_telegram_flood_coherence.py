"""Every outbound Telegram path honours one per-chat flood window.

Upstream arms and consults the window only on the text send path. Edits, typing indicators and
media uploads keep firing requests into a penalty the server is already applying, which lengthens
the penalty that is delaying the real answer. Media additionally had no RetryAfter handling at all,
so a rate-limited attachment surfaced as "couldn't deliver the file attachment" and was never
retried.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


class _FloodError(Exception):
    def __init__(self, seconds: float):
        super().__init__(f"Flood control exceeded. Retry in {seconds} seconds")
        self.retry_after = seconds


def _adapter(**config) -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***", **config))
    adapter._bot = MagicMock()
    return adapter


async def _arm_window(adapter, chat_id="4242", wait=120.0):
    """Drive a real over-cap send refusal so the window is armed the production way."""
    adapter._bot.send_message = AsyncMock(side_effect=_FloodError(wait))
    result = await adapter.send(chat_id, "first answer")
    assert result.success is False and result.error.startswith("flood_control:")
    return result


@pytest.mark.asyncio
async def test_edit_inside_the_window_makes_no_api_call():
    adapter = _adapter()
    await _arm_window(adapter)
    adapter._edit_text = AsyncMock()
    adapter._bot.edit_message_text = AsyncMock()

    result = await adapter.edit_message("4242", "77", "progressive update")

    assert result.success is False
    assert result.error.startswith("flood_control:")
    adapter._edit_text.assert_not_awaited()
    adapter._bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_flood_arms_the_window_for_later_sends(monkeypatch):
    """An edit refusal proves the chat is penalised; the next send must not rediscover that."""
    adapter = _adapter()
    adapter._edit_text = AsyncMock(side_effect=_FloodError(90.0))
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", AsyncMock())

    edit = await adapter.edit_message("4242", "77", "progressive update")
    assert edit.error.startswith("flood_control:")

    adapter._bot.send_message = AsyncMock()
    send = await adapter.send("4242", "a later answer")
    assert send.success is False and send.error.startswith("flood_control:")
    adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_typing_is_suppressed_inside_the_window():
    adapter = _adapter()
    await _arm_window(adapter)
    adapter._bot.send_chat_action = AsyncMock()

    await adapter.send_typing("4242")

    adapter._bot.send_chat_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_retrigger_typing_honours_the_disabled_indicator():
    """The re-arm ignored typing_indicator: false, emitting an unlogged request per send."""
    adapter = _adapter(typing_indicator=False)
    adapter.send_typing = AsyncMock()

    await adapter._retrigger_typing("4242", metadata={})

    adapter.send_typing.assert_not_awaited()


@pytest.mark.asyncio
async def test_media_flood_is_typed_and_arms_the_window(tmp_path, monkeypatch):
    """A rate-limited upload reports flood_control, not a delivery failure."""
    adapter = _adapter()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", AsyncMock())
    adapter._bot.send_document = AsyncMock(side_effect=_FloodError(300.0))
    path = tmp_path / "report.txt"
    path.write_text("payload")

    result = await adapter.send_document("4242", str(path))

    assert result.success is False
    assert result.error.startswith("flood_control:")
    assert "attachment" not in (result.error or "")
    # The window is shared: a following text send refuses locally.
    adapter._bot.send_message = AsyncMock()
    follow_up = await adapter.send("4242", "text after the upload")
    assert follow_up.error.startswith("flood_control:")
    adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_short_media_flood_retries_once_and_succeeds(tmp_path, monkeypatch):
    """Under the inline cap the upload is retried in place, matching the text path."""
    adapter = _adapter()
    slept: list = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", fake_sleep)
    calls = {"n": 0}

    async def flaky(**_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _FloodError(2.0)
        return MagicMock(message_id=99)

    adapter._bot.send_document = AsyncMock(side_effect=flaky)
    path = tmp_path / "report.txt"
    path.write_text("payload")

    result = await adapter.send_document("4242", str(path))

    assert result.success is True and result.message_id == "99"
    assert slept == [2.0] and calls["n"] == 2


@pytest.mark.asyncio
async def test_short_media_flood_wait_holds_other_outbound_traffic(tmp_path):
    """The in-place media retry's wait is a penalty the platform is applying: a concurrent text send
    must not reach Telegram during it, and cosmetic traffic (typing) is suppressed until it ends."""
    adapter = _adapter()
    order: list = []
    first_refused = asyncio.Event()

    async def flaky(**_kw):
        order.append("animation")
        if order.count("animation") == 1:
            first_refused.set()
            raise _FloodError(0.3)
        return MagicMock(message_id=99)

    async def text(**_kw):
        order.append("text")
        return MagicMock(message_id=7)

    adapter._bot.send_animation = AsyncMock(side_effect=flaky)
    adapter._bot.send_message = AsyncMock(side_effect=text)
    adapter._bot.send_chat_action = AsyncMock(side_effect=lambda **_kw: order.append("typing"))
    path = tmp_path / "clip.gif"
    path.write_bytes(b"GIF89a")

    media = asyncio.create_task(adapter.send_animation("4242", str(path)))
    await asyncio.wait_for(first_refused.wait(), timeout=5.0)
    # The refusal propagates back through the media deadline wrapper; wait until the adapter has
    # handled it (armed the window or, before the fix, started its unguarded sleep).
    for _ in range(200):
        if adapter._send_flood_cooldown_remaining("4242") is not None or media.done():
            break
        await asyncio.sleep(0.005)
    await adapter.send_typing("4242")
    follow_up = await asyncio.wait_for(adapter.send("4242", "text during the media wait"), timeout=5.0)
    result = await asyncio.wait_for(media, timeout=5.0)

    assert result.success is True and result.message_id == "99"
    assert follow_up.success is True
    # Nothing reaches Telegram between the refusal and the retry. (send() may re-arm typing after its
    # own message, once the penalty is over; that is allowed.)
    assert order[:3] == ["animation", "animation", "text"], order
    assert adapter._send_flood_cooldown_remaining("4242") is None


@pytest.mark.asyncio
async def test_media_upload_is_refused_locally_inside_an_armed_window(tmp_path):
    adapter = _adapter()
    await _arm_window(adapter)
    adapter._bot.send_document = AsyncMock()
    path = tmp_path / "report.txt"
    path.write_text("payload")

    result = await adapter.send_document("4242", str(path))

    assert result.error.startswith("flood_control:")
    adapter._bot.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_non_flood_media_error_still_reports_a_delivery_failure(tmp_path):
    """Only timing refusals are reclassified; a real upload error keeps its notice."""
    adapter = _adapter()
    adapter._bot.send_document = AsyncMock(side_effect=RuntimeError("file rejected"))
    path = tmp_path / "report.txt"
    path.write_text("payload")

    result = await adapter.send_document("4242", str(path))

    assert result.success is False
    assert not (result.error or "").startswith("flood_control:")


@pytest.mark.asyncio
async def test_the_window_is_per_chat():
    adapter = _adapter()
    await _arm_window(adapter, chat_id="4242")
    adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=7))

    other = await adapter.send("999", "unrelated chat")

    assert other.success is True


@pytest.mark.asyncio
@pytest.mark.parametrize("sender,kwargs,bot_method", [
    ("send_animation", {}, "send_animation"),
])
async def test_other_media_senders_return_typed_flood_result(tmp_path, monkeypatch, sender, kwargs, bot_method):
    """The second I6 review: only document/photo files went through _send_local_file, so a flood
    refusal on voice, animation or image escaped into the generic handlers as a delivery failure."""
    adapter = _adapter()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", AsyncMock())
    setattr(adapter._bot, bot_method, AsyncMock(side_effect=_FloodError(200.0)))
    path = tmp_path / "clip.gif"
    path.write_bytes(b"GIF89a")
    result = await getattr(adapter, sender)("4242", str(path), **kwargs)
    assert result.success is False
    assert (result.error or "").startswith("flood_control:"), result.error


@pytest.mark.asyncio
async def test_media_queued_behind_send_lock_rechecks_flood_cooldown(tmp_path):
    """A queued media send refuses after a text send arms the shared flood window."""
    adapter = _adapter()
    adapter._bot.send_animation = AsyncMock()
    path = tmp_path / "clip.gif"
    path.write_bytes(b"GIF89a")

    async with adapter._chat_send_lock("4242"):
        pending = asyncio.create_task(adapter.send_animation("4242", str(path)))
        await asyncio.sleep(0)
        assert not pending.done()
        adapter._record_send_flood_cooldown("4242", 120.0)

    result = await pending

    assert result.success is False
    assert (result.error or "").startswith("flood_control:")
    adapter._bot.send_animation.assert_not_awaited()


@pytest.mark.asyncio
async def test_album_inside_an_armed_window_returns_the_flood_contract():
    """An album refused locally for flood control must stay reschedulable, not look permanent."""
    adapter = _adapter()
    await _arm_window(adapter, wait=90.0)
    adapter._bot.send_media_group = AsyncMock()
    adapter._bot.send_photo = AsyncMock()

    result = await adapter.send_multiple_images(
        "4242", [("https://example.com/a.png", "a"), ("https://example.com/b.png", "b")])

    assert result.success is False
    assert (result.error or "").startswith("flood_control:"), result.error
    assert result.retry_after is not None and result.retry_after > 60
    adapter._bot.send_media_group.assert_not_awaited()
    adapter._bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_album_refused_by_the_platform_returns_the_flood_contract(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", AsyncMock())
    adapter._bot.send_media_group = AsyncMock(side_effect=_FloodError(200.0))
    adapter._bot.send_photo = AsyncMock()

    result = await adapter.send_multiple_images(
        "4242", [("https://example.com/a.png", "a"), ("https://example.com/b.png", "b")])

    assert result.success is False
    assert (result.error or "").startswith("flood_control:"), result.error
    adapter._bot.send_photo.assert_not_awaited()


def test_timedelta_retry_after_keeps_the_full_penalty():
    from datetime import timedelta

    from plugins.platforms.telegram.adapter import _telegram_retry_after

    err = _FloodError(0)
    err.retry_after = timedelta(seconds=90)
    assert _telegram_retry_after(err) == 90.0


@pytest.mark.asyncio
async def test_album_reports_the_platform_penalty_beyond_the_local_window_cap(monkeypatch):
    """The per-chat window caps at 300s; the album result must still carry Telegram's full deadline."""
    adapter = _adapter()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", AsyncMock())
    adapter._bot.send_media_group = AsyncMock(side_effect=_FloodError(3600.0))
    adapter._bot.send_photo = AsyncMock()

    result = await adapter.send_multiple_images(
        "4242", [("https://example.com/a.png", "a"), ("https://example.com/b.png", "b")])

    assert result.success is False
    assert (result.error or "").startswith("flood_control:"), result.error
    assert result.retry_after is not None and result.retry_after > 3500
    adapter._bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_album_fallback_route_reports_the_platform_penalty(tmp_path, monkeypatch):
    """A non-flood album error falls back per image; a long refusal there must reach the album result.
    Local files keep the route off URL-safety DNS checks."""
    adapter = _adapter()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", AsyncMock())
    adapter._compress_image_to_jpeg = lambda path: None
    adapter._bot.send_media_group = AsyncMock(side_effect=ValueError("bad media group"))
    adapter._bot.send_photo = AsyncMock(side_effect=_FloodError(3600.0))
    adapter._bot.send_document = AsyncMock(side_effect=_FloodError(3600.0))
    images = []
    for name in ("a.png", "b.png"):
        path = tmp_path / name
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        images.append((f"file://{path}", name))

    result = await adapter.send_multiple_images("4242", images)

    assert result.success is False
    assert (result.error or "").startswith("flood_control:"), result.error
    assert result.retry_after is not None and result.retry_after > 3500


@pytest.mark.asyncio
async def test_animation_only_album_reports_the_platform_penalty(tmp_path, monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", AsyncMock())
    adapter._bot.send_animation = AsyncMock(side_effect=_FloodError(3600.0))

    result = await adapter.send_multiple_images("4242", [("https://example.com/a.gif", "a")])

    assert result.success is False
    assert (result.error or "").startswith("flood_control:"), result.error
    assert result.retry_after is not None and result.retry_after > 3500
