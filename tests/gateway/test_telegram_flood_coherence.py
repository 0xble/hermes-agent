"""Every outbound Telegram path honours one per-chat flood window.

Upstream arms and consults the window only on the text send path. Edits, typing indicators and
media uploads keep firing requests into a penalty the server is already applying, which lengthens
the penalty that is delaying the real answer. Media additionally had no RetryAfter handling at all,
so a rate-limited attachment surfaced as "couldn't deliver the file attachment" and was never
retried.
"""

import asyncio
import sqlite3
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
async def test_rich_flood_refuses_following_send_for_full_server_wait(tmp_path):
    adapter = _adapter()
    adapter._update_receipt_dir = tmp_path
    adapter._should_attempt_rich = lambda _content, metadata=None: True
    adapter._bot.do_api_request = AsyncMock(side_effect=_FloodError(3600.0))

    refused = await adapter.send("4242", "first answer")
    assert refused.error.startswith("flood_control:")
    assert refused.retry_after > 3500

    adapter._bot.do_api_request.reset_mock()
    again = await adapter.send("4242", "second answer")
    assert again.error.startswith("flood_control:")
    assert again.retry_after > 3500
    adapter._bot.do_api_request.assert_not_awaited()

    # The deadline belongs to this chat, not every conversation on the bot.
    adapter._bot.do_api_request = AsyncMock(return_value={"message_id": 7})
    other = await adapter.send("999", "other answer")
    assert other.success is True


@pytest.mark.asyncio
async def test_flood_deadline_survives_adapter_replacement(tmp_path):
    first = _adapter()
    first._update_receipt_dir = tmp_path
    first._record_send_flood_cooldown("4242", 3600.0)

    replacement = _adapter()
    replacement._update_receipt_dir = tmp_path
    replacement._bot.send_message = AsyncMock()
    refused = await replacement.send("4242", "reply after restart")

    assert refused.error.startswith("flood_control:")
    assert refused.retry_after > 3500
    replacement._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_persisted_flood_deadline_expires(tmp_path):
    first = _adapter()
    first._update_receipt_dir = tmp_path
    first._record_send_flood_cooldown("4242", 0.02)
    await asyncio.sleep(0.03)

    replacement = _adapter()
    replacement._update_receipt_dir = tmp_path
    replacement._bot.send_message = AsyncMock(return_value=MagicMock(message_id=7))
    result = await replacement.send("4242", "after the deadline")

    assert result.success is True
    replacement._bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_flood_deadline_uses_durable_fallback_when_sqlite_write_fails(tmp_path, monkeypatch):
    first = _adapter()
    first._update_receipt_dir = tmp_path
    monkeypatch.setattr(
        "plugins.platforms.telegram.flood_state.record_deadline",
        lambda *_args: (_ for _ in ()).throw(sqlite3.OperationalError("busy")),
    )
    first._record_send_flood_cooldown("4242", 3600.0)

    replacement = _adapter()
    replacement._update_receipt_dir = tmp_path
    replacement._bot.send_message = AsyncMock()
    refused = await replacement.send("4242", "after replacement")

    assert refused.error.startswith("flood_control:")
    assert refused.retry_after > 3500
    replacement._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_flood_storage_read_error_refuses_outbound_request(tmp_path, monkeypatch):
    adapter = _adapter()
    adapter._update_receipt_dir = tmp_path
    adapter._bot.send_message = AsyncMock()
    monkeypatch.setattr(
        "plugins.platforms.telegram.flood_state.remaining_seconds",
        lambda *_args: (_ for _ in ()).throw(sqlite3.DatabaseError("corrupt")),
    )

    refused = await adapter.send("4242", "answer")

    assert refused.error.startswith("flood_control:")
    adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_rich_edit_flood_arms_shared_window(tmp_path):
    adapter = _adapter()
    adapter._update_receipt_dir = tmp_path
    adapter._bot.do_api_request = AsyncMock(side_effect=_FloodError(3600.0))

    refused = await adapter._try_edit_rich("4242", "77", "finished")
    assert refused.error.startswith("flood_control:")
    adapter._bot.send_message = AsyncMock()
    following = await adapter.send("4242", "later answer")
    assert following.error.startswith("flood_control:")
    adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_final_edit_flood_does_not_try_plain_fallback(tmp_path):
    adapter = _adapter()
    adapter._update_receipt_dir = tmp_path
    adapter._rich_send_disabled = True
    adapter._bot.edit_message_text = AsyncMock(side_effect=_FloodError(3600.0))

    refused = await adapter.edit_message("4242", "77", "finished", finalize=True)

    assert refused.error.startswith("flood_control:")
    assert refused.retry_after > 3500
    adapter._bot.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_overflow_continuation_flood_does_not_try_plain_fallback(tmp_path):
    adapter = _adapter()
    adapter._update_receipt_dir = tmp_path
    adapter._rich_send_disabled = True
    adapter._bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=77))
    adapter._bot.send_message = AsyncMock(side_effect=_FloodError(3600.0))

    refused = await adapter.edit_message("4242", "77", "word " * 1100, finalize=True)

    assert refused.error.startswith("flood_control:")
    assert refused.retry_after > 3500
    assert refused.raw_response["partial_overflow"] is True
    adapter._bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_rechecks_flood_after_pacing_wait(tmp_path, monkeypatch):
    adapter = _adapter()
    adapter._update_receipt_dir = tmp_path
    adapter._rich_send_disabled = True
    adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=7))
    adapter._chat_outbound_slot_remaining = lambda _chat_id: 1.0
    sleeping = asyncio.Event()
    resume = asyncio.Event()

    async def paused_sleep(_delay):
        sleeping.set()
        await resume.wait()

    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", paused_sleep)
    pending = asyncio.create_task(adapter.send("4242", "answer"))
    await sleeping.wait()
    adapter._record_send_flood_cooldown("4242", 3600.0)
    resume.set()
    refused = await pending

    assert refused.error.startswith("flood_control:")
    adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_short_edit_retry_stops_when_deadline_grows_during_wait(tmp_path, monkeypatch):
    adapter = _adapter()
    adapter._update_receipt_dir = tmp_path
    adapter._rich_send_disabled = True
    adapter._bot.edit_message_text = AsyncMock(side_effect=_FloodError(0.05))
    sleeping = asyncio.Event()
    resume = asyncio.Event()

    async def paused_sleep(_delay):
        sleeping.set()
        await resume.wait()

    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", paused_sleep)
    pending = asyncio.create_task(adapter.edit_message("4242", "77", "finished", finalize=True))
    await sleeping.wait()
    adapter._record_send_flood_cooldown("4242", 3600.0)
    resume.set()
    refused = await pending

    assert refused.error.startswith("flood_control:")
    assert refused.retry_after > 3500
    adapter._bot.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_network_retry_stops_when_deadline_appears_during_wait(tmp_path, monkeypatch):
    from telegram.error import NetworkError

    adapter = _adapter()
    adapter._update_receipt_dir = tmp_path
    adapter._rich_send_disabled = True
    adapter._bot.send_message = AsyncMock(side_effect=[NetworkError("connection lost"), MagicMock(message_id=7)])
    sleeping = asyncio.Event()
    resume = asyncio.Event()

    async def paused_sleep(_delay):
        sleeping.set()
        await resume.wait()

    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", paused_sleep)
    pending = asyncio.create_task(adapter.send("4242", "answer"))
    await sleeping.wait()
    adapter._record_send_flood_cooldown("4242", 3600.0)
    resume.set()
    refused = await pending

    assert refused.error.startswith("flood_control:")
    adapter._bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_typing_is_suppressed_inside_the_window():
    adapter = _adapter()
    await _arm_window(adapter)
    adapter._bot.send_chat_action = AsyncMock()

    await adapter.send_typing("4242")

    adapter._bot.send_chat_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_typing_flood_arms_shared_window_without_topic_fallback(tmp_path):
    adapter = _adapter()
    adapter._update_receipt_dir = tmp_path
    adapter._bot.send_chat_action = AsyncMock(side_effect=_FloodError(3600.0))
    adapter._dm_topic_fallback = lambda _metadata: True
    adapter._message_thread_id_for_typing = lambda _thread_id: 77

    await adapter.send_typing("4242")

    assert adapter._bot.send_chat_action.await_count == 1
    adapter._bot.send_message = AsyncMock()
    refused = await adapter.send("4242", "answer")
    assert refused.error.startswith("flood_control:")
    assert refused.retry_after > 3500
    adapter._bot.send_message.assert_not_awaited()


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
async def test_short_media_flood_retries_once_and_succeeds(tmp_path):
    """Under the inline cap the upload is retried in place, matching the text path."""
    adapter = _adapter()
    calls = {"n": 0}

    async def flaky(**_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _FloodError(0.02)
        return MagicMock(message_id=99)

    adapter._bot.send_document = AsyncMock(side_effect=flaky)
    path = tmp_path / "report.txt"
    path.write_text("payload")

    result = await adapter.send_document("4242", str(path))

    assert result.success is True and result.message_id == "99"
    assert calls["n"] == 2


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


@pytest.mark.asyncio
async def test_drafts_controls_and_deletes_share_known_flood_window():
    adapter = _adapter()
    await _arm_window(adapter)
    adapter._bot.send_message_draft = AsyncMock(return_value=True)
    adapter._bot.send_message = AsyncMock()
    adapter._bot.delete_message = AsyncMock(return_value=True)
    adapter._status_message_ids = {("4242", "topic", "status"): "77"}
    adapter._should_attempt_rich_draft = lambda _content: False
    result = await adapter.send_draft("4242", 1, "preview")
    assert result.error.startswith("flood_control:") and result.retry_after > 0
    result = await adapter._send_prompt("probe", "4242", {}, lambda: ("control", None, None))
    assert result.error.startswith("flood_control:") and result.retry_after > 0
    assert await adapter.delete_message("4242", "77") is False
    adapter._bot.send_message_draft.assert_not_awaited()
    adapter._bot.send_message.assert_not_awaited()
    adapter._bot.delete_message.assert_not_awaited()
    assert "77" in adapter._status_message_ids.values()
    # The refusal did not erase ownership; cleanup can retry after the deadline.
    # A draft queued behind another outbound call must recheck the newly armed window.
    async with adapter._chat_send_lock("4242"):
        queued = asyncio.create_task(adapter.send_draft("4242", 2, "later preview"))
        await asyncio.sleep(0)
        adapter._record_send_flood_cooldown("4242", 90)
    assert (await queued).error.startswith("flood_control:")
    adapter._bot.send_message_draft.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["draft", "rich_draft", "control", "delete"])
async def test_auxiliary_outbound_flood_arms_other_surfaces_without_fallback(surface):
    from datetime import timedelta
    adapter = _adapter()
    adapter._should_attempt_rich_draft = lambda _content: surface == "rich_draft"
    refusal = _FloodError(120)
    refusal.retry_after = timedelta(seconds=120)
    adapter._bot.send_message_draft = AsyncMock(side_effect=refusal)
    adapter._bot.do_api_request = AsyncMock(side_effect=refusal)
    adapter._bot.send_message = AsyncMock(side_effect=refusal)
    adapter._bot.delete_message = AsyncMock(side_effect=refusal)
    if surface in ("draft", "rich_draft"):
        result = await adapter.send_draft("4242", 1, "preview")
        assert result.error.startswith("flood_control:") and result.retry_after > 0
        if surface == "rich_draft":
            adapter._bot.send_message_draft.assert_not_awaited()
    elif surface == "control":
        result = await adapter._send_prompt("probe", "4242", {}, lambda: ("control", None, None))
        assert result.error.startswith("flood_control:") and result.retry_after == 120
    else:
        assert await adapter.delete_message("4242", "77") is False
    adapter._bot.send_chat_action = AsyncMock()
    await adapter.send_typing("4242")
    adapter._bot.send_chat_action.assert_not_awaited()
    assert adapter._send_flood_cooldown_remaining("4242") > 0
