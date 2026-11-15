"""Auto-transfer must create a useful Telegram label without relying on a later rename."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.gateway.test_telegram_dm_topic_auto_transfer import (
    NEW_TOPIC_ID,
    _deliver_root_dm,
    _reply_calls,
    _rig,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "opening, expected",
    [
        (
            "Plan the database migration\nKeep rollback available",
            "Plan the database migration",
        ),
        ("Réparer les notifications Telegram", "Réparer les notifications Telegram"),
    ],
)
async def test_auto_transfer_creates_opening_title_without_later_rename(
    tmp_path,
    monkeypatch,
    opening,
    expected,
):
    import agent.title_generator as titles

    model_title = MagicMock(
        side_effect=AssertionError("transfer must not call a model")
    )
    monkeypatch.setattr(titles, "generate_title", model_title)
    _runner, adapter, db, runs = _rig(
        tmp_path, monkeypatch, extra={"dm_topic_auto_transfer": True}
    )
    adapter._bot.edit_forum_topic = AsyncMock()
    try:
        event = await _deliver_root_dm(adapter, text=opening)

        adapter._bot.create_forum_topic.assert_awaited_once_with(
            chat_id=208214988,
            name=expected,
        )
        adapter._bot.edit_forum_topic.assert_not_awaited()
        model_title.assert_not_called()
        assert event.source.thread_id == str(NEW_TOPIC_ID)
        assert len(runs) == 1
        (reply,) = _reply_calls(adapter)
        assert reply.kwargs.get("message_thread_id") == NEW_TOPIC_ID
        assert reply.kwargs.get("reply_to_message_id") is None
        binding = db.get_telegram_topic_binding(
            chat_id="208214988", thread_id=str(NEW_TOPIC_ID)
        )
        assert binding is not None and binding["user_id"] == "208214988"
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("title_fails", [False, True])
async def test_auto_transfer_without_usable_title_preserves_media_event(
    tmp_path, monkeypatch, title_fails
):
    import agent.title_generator as titles
    from gateway.platforms.event import MessageEvent, MessageType
    from agent.i18n import t
    from plugins.platforms.telegram.telegram_topic_transfer import (
        apply_root_dm_topic_transfer,
    )
    from tests.gateway.test_telegram_topic_mode import _make_source

    if title_fails:
        monkeypatch.setattr(
            titles,
            "derive_title",
            MagicMock(side_effect=RuntimeError("title unavailable")),
        )
    _runner, adapter, db, _runs = _rig(
        tmp_path, monkeypatch, extra={"dm_topic_auto_transfer": True}
    )
    event = MessageEvent(
        text="caption" if title_fails else "",
        source=_make_source(),
        message_id="4242",
        message_type=MessageType.PHOTO,
        media_urls=["https://example.test/photo.jpg"],
    )
    try:
        await apply_root_dm_topic_transfer(adapter, event)

        adapter._bot.create_forum_topic.assert_awaited_once_with(
            chat_id=208214988,
            name=t("gateway.topic.default_title"),
        )
        assert event.source.thread_id == str(NEW_TOPIC_ID)
        assert event.source.user_id == "208214988"
        assert event.message_id == "4242"
        assert event.media_urls == ["https://example.test/photo.jpg"]
        assert event.reply_anchor_override == ""
    finally:
        db.close()
