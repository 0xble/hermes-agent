from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from hermes_state import SessionDB, AsyncSessionDB


def _event(text, thread_id="42"):
    return MessageEvent(text=text, source=SessionSource(platform=Platform.TELEGRAM, user_id="u", chat_id="c", user_name="u", chat_type="dm", thread_id=thread_id), message_id="m")


def _runner(tmp_path):
    from tests.gateway.test_telegram_topic_mode import _make_runner
    db = SessionDB(tmp_path / "state.db")
    db.enable_telegram_topic_mode(chat_id="c", user_id="u")
    db.create_session("sess-topic", source="telegram", user_id="u")
    db.set_session_title("sess-topic", "Old name")
    db.bind_telegram_topic(chat_id="c", thread_id="42", user_id="u", session_key="k", session_id="sess-topic")
    runner = _make_runner(db)
    runner._session_db = AsyncSessionDB(db)
    runner._telegram_topic_mode_enabled = lambda source: True
    return runner, db


@pytest.mark.asyncio
async def test_topic_edit_routes_title_and_icon_together(tmp_path):
    runner, db = _runner(tmp_path)
    adapter = runner.adapters[Platform.TELEGRAM]
    adapter.rename_dm_topic = AsyncMock(return_value=True)
    adapter.get_forum_topic_icon_options = AsyncMock(return_value=[{"emoji": "💳", "custom_emoji_id": "card-id"}])
    result = await runner._handle_topic_command(_event('/topic edit --title "Hermes Payment Vault" --icon 💳'))
    assert "Hermes Payment Vault" in result
    adapter.rename_dm_topic.assert_awaited_once_with(chat_id="c", thread_id="42", name="Hermes Payment Vault", icon_custom_emoji_id="card-id")
    assert db.get_telegram_topic_icon_state("c", "42")["owner"] == "manual"
    db.close()


@pytest.mark.asyncio
async def test_topic_edit_rejects_unknown_icon(tmp_path):
    runner, db = _runner(tmp_path)
    runner.adapters[Platform.TELEGRAM].get_forum_topic_icon_options = AsyncMock(return_value=[{"emoji": "💳", "custom_emoji_id": "card-id"}])
    result = await runner._handle_topic_command(_event('/topic edit --title x --icon 🚀'))
    assert "Unsupported topic icon" in result
    db.close()
