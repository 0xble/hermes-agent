"""Stable owner routing for recurring Telegram named-topic delivery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes_state import SessionDB
from tests.gateway.conftest import _ensure_telegram_mock

_ensure_telegram_mock()
from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


@pytest.fixture
def db(tmp_path):
    value = SessionDB(tmp_path / "state.db")
    yield value
    value.close()


def _adapter(db, *, create_result=444):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***", extra={}))
    adapter._session_store = SimpleNamespace(_db=db)
    adapter._bot = AsyncMock()
    adapter._bot.create_forum_topic.return_value = SimpleNamespace(message_thread_id=create_result)
    adapter._persist_dm_topic_thread_id = MagicMock()
    return adapter


def _live_session(db, session_id, title, thread_id, *, managed_mode="auto"):
    db.create_session(session_id, source="telegram")
    db.set_session_title(session_id, title)
    db.bind_telegram_topic(
        chat_id="123", thread_id=str(thread_id), user_id="123",
        session_key=f"agent:main:telegram:dm:123:{thread_id}", session_id=session_id,
        managed_mode=managed_mode,
    )


@pytest.mark.asyncio
async def test_same_title_backstop_claims_one_existing_live_owner(db):
    _live_session(db, "existing", "Meridian Gmail Watcher", 324637, managed_mode="automation")
    adapter = _adapter(db)

    result = await adapter.ensure_dm_topic(
        "123", "Meridian Gmail Watcher", owner_key="subject:meridian-gmail-watcher")

    assert result == "324637"
    assert adapter._bot.create_forum_topic.await_count == 0
    owner = db.get_telegram_topic_owner(chat_id="123", owner_key="subject:meridian-gmail-watcher")
    assert owner["status"] == "active"
    assert owner["thread_id"] == "324637"


@pytest.mark.asyncio
async def test_ambiguous_live_same_titles_fail_closed_without_creation(db, monkeypatch):
    _live_session(db, "one", "Same subject", 10)
    candidates = [
        {"thread_id": "10", "session_id": "one", "title": "Same subject"},
        {"thread_id": "11", "session_id": "two", "title": "Same subject"},
    ]
    monkeypatch.setattr(db, "list_live_telegram_topic_title_candidates", lambda **_: candidates)
    adapter = _adapter(db)

    with pytest.raises(RuntimeError, match="ambiguous"):
        await adapter.ensure_dm_topic("123", "Same subject", owner_key="subject:same")

    assert adapter._bot.create_forum_topic.await_count == 0
    owner = db.get_telegram_topic_owner(chat_id="123", owner_key="subject:same")
    assert owner["status"] == "conflict"


@pytest.mark.asyncio
async def test_user_owned_same_title_is_not_adopted(db):
    _live_session(db, "user-owned", "Same subject", 10, managed_mode="auto")
    adapter = _adapter(db)

    with pytest.raises(RuntimeError, match="user-owned"):
        await adapter.ensure_dm_topic("123", "Same subject", owner_key="job:same")

    assert adapter._bot.create_forum_topic.await_count == 0
    assert db.get_telegram_topic_owner(chat_id="123", owner_key="job:same")["status"] == "conflict"


@pytest.mark.asyncio
async def test_concurrent_reservation_has_one_creator(db):
    first = db.reserve_telegram_topic_owner(chat_id="chat", owner_key="job:x", topic_name="X")
    second = db.reserve_telegram_topic_owner(chat_id="chat", owner_key="job:x", topic_name="X")

    assert first["_reserved"] is True
    assert second["_reserved"] is False
    assert second["status"] == "creating"


@pytest.mark.asyncio
async def test_ambiguous_remote_create_marks_owner_unresolved(db):
    adapter = _adapter(db)
    adapter._create_dm_topic = AsyncMock(side_effect=TimeoutError("create response lost"))

    with pytest.raises(TimeoutError, match="create response lost"):
        await adapter.ensure_dm_topic("123", "Lost create", owner_key="job:lost-create")

    owner = db.get_telegram_topic_owner(chat_id="123", owner_key="job:lost-create")
    assert owner["status"] == "unresolved"
    assert adapter._create_dm_topic.await_count == 1


@pytest.mark.asyncio
async def test_explicit_parallel_mode_does_not_claim_existing_owner(db):
    _live_session(db, "existing", "Same subject", 10)
    adapter = _adapter(db, create_result=12)
    adapter._dm_topics["123:Same subject"] = 10

    result = await adapter.ensure_dm_topic(
        "123", "Same subject", owner_key="subject:same:parallel-2", allow_parallel=True)

    assert result == "12"
    assert adapter._bot.create_forum_topic.await_count == 1
    assert adapter._dm_topics["123:Same subject"] == 10
    owner = db.get_telegram_topic_owner(
        chat_id="123", owner_key="subject:same:parallel-2")
    assert owner["status"] == "active"
    assert owner["thread_id"] == "12"


@pytest.mark.asyncio
async def test_parallel_owner_reuses_its_own_lane(db):
    adapter = _adapter(db, create_result=12)

    first = await adapter.ensure_dm_topic(
        "123", "Same subject", owner_key="subject:same:parallel-2", allow_parallel=True)
    second = await adapter.ensure_dm_topic(
        "123", "Same subject", owner_key="subject:same:parallel-2", allow_parallel=True)

    assert first == second == "12"
    assert adapter._bot.create_forum_topic.await_count == 1


def test_owner_state_survives_restart_and_retired_owner_is_not_active(db, tmp_path):
    db_path = tmp_path / "state.db"
    db.reserve_telegram_topic_owner(chat_id="123", owner_key="job:x", topic_name="X")
    db.set_telegram_topic_owner_active(
        chat_id="123", owner_key="job:x", thread_id="99", topic_name="X")
    db.close()
    reopened = SessionDB(db_path)
    try:
        owner = reopened.get_telegram_topic_owner(chat_id="123", owner_key="job:x")
        assert owner["status"] == "active"
        assert owner["thread_id"] == "99"
        reopened.set_telegram_topic_owner_state(
            chat_id="123", owner_key="job:x", status="retired",
            canonical_owner_key="job:y")
        assert reopened.get_telegram_topic_owner(
            chat_id="123", owner_key="job:x")["canonical_owner_key"] == "job:y"
    finally:
        reopened.close()


def test_numeric_topic_delivery_claims_existing_live_binding(db):
    _live_session(db, "existing", "Meridian Gmail Watcher", 324637)
    adapter = _adapter(db)

    adapter.ensure_existing_telegram_topic_owner("123", "324637", "cron:watcher")

    owner = db.get_telegram_topic_owner(chat_id="123", owner_key="cron:watcher")
    assert owner["status"] == "active"
    assert owner["thread_id"] == "324637"


def test_numeric_topic_without_binding_fails_closed(db):
    adapter = _adapter(db)
    with pytest.raises(RuntimeError, match="no live Hermes binding"):
        adapter.ensure_existing_telegram_topic_owner("123", "345203", "cron:watcher")
