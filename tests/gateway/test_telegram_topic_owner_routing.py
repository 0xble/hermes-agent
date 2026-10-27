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


@pytest.mark.asyncio
async def test_named_shared_owner_resolves_canonical_thread(db):
    _live_session(db, "existing", "Same subject", 10, managed_mode="automation")
    db.reserve_telegram_topic_owner(chat_id="123", owner_key="job:first", topic_name="Same subject")
    db.set_telegram_topic_owner_active(
        chat_id="123", owner_key="job:first", thread_id="10", topic_name="Same subject")
    db.reserve_telegram_topic_owner(chat_id="123", owner_key="job:second", topic_name="Same subject")
    db.set_telegram_topic_owner_shared(
        chat_id="123", owner_key="job:second", canonical_owner_key="job:first",
        topic_name="Same subject")

    adapter = _adapter(db)
    result = await adapter.ensure_dm_topic("123", "Same subject", owner_key="job:second")

    assert result == "10"
    assert adapter._bot.create_forum_topic.await_count == 0


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


def test_second_numeric_topic_owner_shares_existing_owner(db):
    _live_session(db, "existing", "Meridian Gmail Watcher", 324637)
    adapter = _adapter(db)

    adapter.ensure_existing_telegram_topic_owner("123", "324637", "cron:first")
    adapter.ensure_existing_telegram_topic_owner("123", "324637", "cron:second")

    first = db.get_telegram_topic_owner(chat_id="123", owner_key="cron:first")
    second = db.get_telegram_topic_owner(chat_id="123", owner_key="cron:second")
    assert first["status"] == "active"
    assert first["thread_id"] == "324637"
    assert second["status"] == "active"
    assert second["thread_id"] is None
    assert second["canonical_owner_key"] == "cron:first"


@pytest.mark.parametrize("stuck_status", ["creating", "conflict", "retired"])
def test_stuck_numeric_topic_owner_shares_existing_active_owner(db, monkeypatch, stuck_status):
    _live_session(db, "existing", "Meridian Gmail Watcher", 324637)
    db.reserve_telegram_topic_owner(chat_id="123", owner_key="cron:first", topic_name="X")
    db.set_telegram_topic_owner_active(
        chat_id="123", owner_key="cron:first", thread_id="324637", topic_name="X")
    db.reserve_telegram_topic_owner(chat_id="123", owner_key="cron:second", topic_name="X")
    if stuck_status != "creating":
        db.set_telegram_topic_owner_state(
            chat_id="123", owner_key="cron:second", status=stuck_status,
            canonical_owner_key="cron:first")
    if stuck_status == "retired":
        monkeypatch.setattr(db, "get_telegram_topic_owner_for_thread", lambda **_: None)

    _adapter(db).ensure_existing_telegram_topic_owner("123", "324637", "cron:second")

    second = db.get_telegram_topic_owner(chat_id="123", owner_key="cron:second")
    assert second["status"] == "active"
    assert second["thread_id"] is None
    assert second["canonical_owner_key"] == "cron:first"


def test_racing_first_numeric_topic_claim_shares_after_activation_conflict(db, monkeypatch):
    _live_session(db, "existing", "Meridian Gmail Watcher", 324637)
    db.reserve_telegram_topic_owner(chat_id="123", owner_key="cron:first", topic_name="X")
    db.set_telegram_topic_owner_active(
        chat_id="123", owner_key="cron:first", thread_id="324637", topic_name="X")
    original = db.get_telegram_topic_owner_for_thread
    calls = 0

    def hide_owner_for_first_race_check(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return original(**kwargs)

    monkeypatch.setattr(db, "get_telegram_topic_owner_for_thread", hide_owner_for_first_race_check)
    _adapter(db).ensure_existing_telegram_topic_owner("123", "324637", "cron:second")

    second = db.get_telegram_topic_owner(chat_id="123", owner_key="cron:second")
    assert second["status"] == "active"
    assert second["thread_id"] is None
    assert second["canonical_owner_key"] == "cron:first"


def test_conflicting_activation_retires_creating_claim(db):
    db.reserve_telegram_topic_owner(chat_id="123", owner_key="cron:first", topic_name="X")
    db.set_telegram_topic_owner_active(
        chat_id="123", owner_key="cron:first", thread_id="324637", topic_name="X")
    db.reserve_telegram_topic_owner(chat_id="123", owner_key="cron:second", topic_name="X")

    with pytest.raises(RuntimeError, match="already owned"):
        db.set_telegram_topic_owner_active(
            chat_id="123", owner_key="cron:second", thread_id="324637", topic_name="X")

    second = db.get_telegram_topic_owner(chat_id="123", owner_key="cron:second")
    assert second["status"] == "conflict"


def _two_owned_threads(db):
    _live_session(db, "first-session", "First", 100)
    _live_session(db, "third-session", "Third", 200)
    for owner_key, thread_id in (("cron:first", "100"), ("cron:third", "200")):
        db.reserve_telegram_topic_owner(chat_id="123", owner_key=owner_key, topic_name="X")
        db.set_telegram_topic_owner_active(
            chat_id="123", owner_key=owner_key, thread_id=thread_id, topic_name="X")


def test_shared_owner_is_refused_on_thread_held_by_another_owner(db):
    _two_owned_threads(db)
    adapter = _adapter(db)
    adapter.ensure_existing_telegram_topic_owner("123", "100", "cron:second")

    with pytest.raises(RuntimeError, match="refusing reroute"):
        adapter.ensure_existing_telegram_topic_owner("123", "200", "cron:second")

    second = db.get_telegram_topic_owner(chat_id="123", owner_key="cron:second")
    assert second["canonical_owner_key"] == "cron:first"


@pytest.mark.parametrize("stuck_status", ["conflict", "retired"])
def test_stuck_owner_is_refused_on_thread_held_by_unrelated_owner(db, stuck_status):
    _two_owned_threads(db)
    db.reserve_telegram_topic_owner(chat_id="123", owner_key="cron:second", topic_name="X")
    db.set_telegram_topic_owner_state(
        chat_id="123", owner_key="cron:second", status=stuck_status,
        canonical_owner_key="cron:first")

    with pytest.raises(RuntimeError, match="refusing reroute"):
        _adapter(db).ensure_existing_telegram_topic_owner("123", "200", "cron:second")

    second = db.get_telegram_topic_owner(chat_id="123", owner_key="cron:second")
    assert second["status"] == stuck_status
    assert second["canonical_owner_key"] == "cron:first"


@pytest.mark.parametrize("stuck_status", ["conflict", "retired"])
def test_stuck_owner_without_canonical_is_not_bound_to_thread_owner(db, stuck_status):
    _two_owned_threads(db)
    db.reserve_telegram_topic_owner(chat_id="123", owner_key="cron:second", topic_name="X")
    db.set_telegram_topic_owner_state(
        chat_id="123", owner_key="cron:second", status=stuck_status)

    with pytest.raises(RuntimeError, match="refusing reroute"):
        _adapter(db).ensure_existing_telegram_topic_owner("123", "200", "cron:second")

    second = db.get_telegram_topic_owner(chat_id="123", owner_key="cron:second")
    assert second["status"] == stuck_status
    assert not second["canonical_owner_key"]


def test_creating_reservation_for_another_topic_is_not_shared_onto_owned_thread(db):
    _two_owned_threads(db)
    db.reserve_telegram_topic_owner(chat_id="123", owner_key="cron:second", topic_name="Other lane")

    with pytest.raises(RuntimeError, match="refusing reroute"):
        _adapter(db).ensure_existing_telegram_topic_owner("123", "200", "cron:second")

    second = db.get_telegram_topic_owner(chat_id="123", owner_key="cron:second")
    assert second["status"] == "creating"
    assert not second["canonical_owner_key"]


@pytest.mark.asyncio
async def test_retirement_merge_redirects_old_and_shared_owner_delivery_after_recovery(db):
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.telegram_retire import save
    from gateway.telegram_retire_prepare import redirect_owner
    from hermes_state import AsyncSessionDB

    _live_session(db, "old", "Old Service", 10, managed_mode="automation")
    _live_session(db, "next", "Service Operations", 20, managed_mode="automation")
    db.reserve_telegram_topic_owner(chat_id="123", owner_key="job:old", topic_name="Old Service")
    db.set_telegram_topic_owner_active(chat_id="123", owner_key="job:old", thread_id="10", topic_name="Old Service")
    db.reserve_telegram_topic_owner(chat_id="123", owner_key="job:shared", topic_name="Shared Service")
    db.set_telegram_topic_owner_shared(chat_id="123", owner_key="job:shared", canonical_owner_key="job:old",
                                       topic_name="Shared Service")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", thread_id="10", chat_type="dm")
    runner = SimpleNamespace(_session_db=AsyncSessionDB(db))
    state = {"generation": "merge", "phase": "deleting", "session_id": "old",
             "merge_destination": {"thread_id": "20", "session_id": "next", "title": "Service Operations"}}
    await save(runner, source, state)
    await redirect_owner(runner, source, state)
    await redirect_owner(runner, source, state)
    db.delete_telegram_topic_binding(chat_id="123", thread_id="10")
    adapter = _adapter(db)
    assert await adapter.ensure_dm_topic("123", "Old Service", owner_key="job:old") == "20"
    assert await adapter.ensure_dm_topic("123", "Shared Service", owner_key="job:shared") == "20"
    adapter._bot.create_forum_topic.assert_not_awaited()
    assert db.get_session("old") is not None
    db.delete_telegram_topic_binding(chat_id="123", thread_id="20")
    with pytest.raises(RuntimeError, match="no live Hermes binding"):
        await adapter.ensure_dm_topic("123", "Old Service", owner_key="job:old")
    adapter._bot.create_forum_topic.assert_not_awaited()


def test_shared_owner_cycles_fail_closed_instead_of_creating_a_topic(db):
    for key in ("a", "b"):
        db.reserve_telegram_topic_owner(chat_id="123", owner_key=key, topic_name=key)
    db.set_telegram_topic_owner_shared(chat_id="123", owner_key="a", canonical_owner_key="b", topic_name="a")
    db.set_telegram_topic_owner_shared(chat_id="123", owner_key="b", canonical_owner_key="a", topic_name="b")
    with pytest.raises(RuntimeError, match="redirect cycle"):
        db.resolve_telegram_topic_owner(chat_id="123", owner_key="a")
    assert db.resolve_telegram_topic_owner(chat_id="123", owner_key="a", profile_name="secondary") is None


def test_numeric_topic_without_binding_fails_closed(db):
    adapter = _adapter(db)
    with pytest.raises(RuntimeError, match="no live Hermes binding"):
        adapter.ensure_existing_telegram_topic_owner("123", "345203", "cron:watcher")
