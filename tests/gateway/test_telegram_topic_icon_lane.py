"""The Telegram topic rename lane runs on the gateway loop, where ``runner._session_db`` is the
``AsyncSessionDB`` facade. Every DB call in that lane must go through the sync handle inside
``asyncio.to_thread`` (or be awaited on the facade); wrapping a facade attribute in
``to_thread`` yields an un-awaited coroutine and the icon path dies silently.

These tests drive the real ``GatewayRunner`` rename lane against a real ``SessionDB`` behind the
real facade, with only the Telegram transport faked."""

from __future__ import annotations

import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_state import AsyncSessionDB, SessionDB

CHAT, THREAD, USER = "208214988", "77", "208214988"


def _source(thread_id: str = THREAD) -> SessionSource:
    return SessionSource(platform=Platform.TELEGRAM, user_id=USER, chat_id=CHAT, user_name="t",
                         chat_type="dm", thread_id=thread_id)


def _runner(tmp_path, *, extra: dict, session_id: str = "sess-1"):
    db = SessionDB(tmp_path / "state.db")
    db.create_session(session_id, source="telegram")
    db.enable_telegram_topic_mode(chat_id=CHAT, user_id=USER)
    db.bind_telegram_topic(chat_id=CHAT, thread_id=THREAD, user_id=USER, session_key="k",
                           session_id=session_id)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***", extra=extra)})
    adapter = MagicMock()
    adapter._bot = None
    adapter.rename_dm_topic = AsyncMock(return_value=True)
    adapter.get_forum_topic_icon_options = AsyncMock(return_value=[
        {"emoji": "🔥", "custom_emoji_id": "id-bug"},
        {"emoji": "📈", "custom_emoji_id": "id-chart"},
    ])
    adapter._forum_topic_icon_options = None
    type(adapter).get_manual_topic_icon = None  # not a TelegramAdapter; no manual-icon source
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._session_db = AsyncSessionDB(db)  # the facade, exactly as the gateway holds it
    return runner, adapter, db


@pytest.mark.anyio
async def test_icon_lane_reads_and_writes_through_the_real_async_facade(tmp_path):
    runner, adapter, db = _runner(tmp_path, extra={"auto_topic_icons": True})
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)  # "coroutine ... was never awaited"
        await runner._rename_telegram_topic_for_session_title(
            _source(), "sess-1", "Fix auth bug", user_message="debug the login test failure")

    adapter.rename_dm_topic.assert_awaited_once()
    kwargs = adapter.rename_dm_topic.await_args.kwargs
    assert kwargs["name"] == "Fix auth bug"
    assert kwargs["icon_custom_emoji_id"] == "id-bug", "name and icon must ride one Bot API call"
    # State and history landed only after the confirmed rename, and are readable via the sync DB.
    assert db.get_telegram_topic_icon_state(CHAT, THREAD)["owner"] == "auto"
    assert db.list_recent_telegram_topic_icons(CHAT) == ["🔥"]


@pytest.mark.anyio
async def test_failed_rename_records_no_icon_state(tmp_path):
    runner, adapter, db = _runner(tmp_path, extra={"auto_topic_icons": True})
    adapter.rename_dm_topic = AsyncMock(side_effect=RuntimeError("telegram down"))
    await runner._rename_telegram_topic_for_session_title(_source(), "sess-1", "Fix auth bug")
    assert db.get_telegram_topic_icon_state(CHAT, THREAD) is None
    assert db.list_recent_telegram_topic_icons(CHAT) == []


@pytest.mark.anyio
async def test_model_icon_wins_over_heuristic_when_allowed(tmp_path):
    runner, adapter, _ = _runner(tmp_path, extra={"auto_topic_icons": True})
    await runner._rename_telegram_topic_for_session_title(
        _source(), "sess-1", "Fix auth bug", user_message="debug", model_icon="📈")
    assert adapter.rename_dm_topic.await_args.kwargs["icon_custom_emoji_id"] == "id-chart"


@pytest.mark.anyio
async def test_rename_skips_when_binding_belongs_to_another_session(tmp_path):
    runner, adapter, _ = _runner(tmp_path, extra={"auto_topic_icons": True}, session_id="owner")
    await runner._rename_telegram_topic_for_session_title(_source(), "stale-session", "Whatever")
    adapter.rename_dm_topic.assert_not_awaited()


def test_icon_context_is_none_until_the_adapter_has_cached_the_catalog(tmp_path):
    runner, adapter, _ = _runner(tmp_path, extra={"auto_topic_icons": True,
                                                  "topic_icon_instructions": "playful"})
    assert runner._telegram_topic_icon_context(_source()) is None
    adapter._forum_topic_icon_options = [{"emoji": "🔥", "custom_emoji_id": "id-bug"}]
    ctx = runner._telegram_topic_icon_context(_source())
    assert ctx["options"] == [{"emoji": "🔥", "custom_emoji_id": "id-bug"}]
    assert ctx["instructions"] == "playful"


@pytest.mark.anyio
async def test_unmatched_topic_keeps_its_icon_instead_of_getting_an_arbitrary_one(tmp_path):
    runner, adapter, db = _runner(tmp_path, extra={"auto_topic_icons": True})
    await runner._rename_telegram_topic_for_session_title(_source(), "sess-1", "Quiet Afternoon", user_message="hello")
    kwargs = adapter.rename_dm_topic.await_args.kwargs
    assert kwargs["name"] == "Quiet Afternoon"
    assert "icon_custom_emoji_id" not in kwargs
    assert db.get_telegram_topic_icon_state(CHAT, THREAD) is None


@pytest.mark.anyio
async def test_recently_used_icon_is_still_chosen_when_it_is_the_match(tmp_path):
    runner, adapter, db = _runner(tmp_path, extra={"auto_topic_icons": True})
    db.record_telegram_topic_icon_history(CHAT, emoji="🔥", custom_emoji_id="id-bug")
    await runner._rename_telegram_topic_for_session_title(
        _source(), "sess-1", "Fix auth bug", user_message="debug the crash")
    assert adapter.rename_dm_topic.await_args.kwargs["icon_custom_emoji_id"] == "id-bug"


@pytest.mark.anyio
async def test_explicit_title_changes_icon_in_the_same_rename(tmp_path, monkeypatch):
    """/title picks an icon for the user's title and sends it with the name in one Bot API call."""
    runner, adapter, db = _runner(tmp_path, extra={"auto_topic_icons": True})
    seen = {}

    def fake_pick(title, options, **kwargs):
        seen["title"] = title
        return "📈"

    monkeypatch.setattr("agent.title_generator.pick_topic_icon", fake_pick)
    assert await runner._rename_telegram_topic_explicit(_source(), "sess-1", "Quarterly Revenue") is True

    adapter.rename_dm_topic.assert_awaited_once()
    kwargs = adapter.rename_dm_topic.await_args.kwargs
    assert (kwargs["name"], kwargs["icon_custom_emoji_id"]) == ("Quarterly Revenue", "id-chart")
    assert seen["title"] == "Quarterly Revenue"
    assert db.get_telegram_topic_icon_state(CHAT, THREAD)["emoji"] == "📈"


@pytest.mark.anyio
async def test_explicit_title_keeps_a_manually_chosen_icon(tmp_path, monkeypatch):
    runner, adapter, db = _runner(tmp_path, extra={"auto_topic_icons": True})
    db.record_telegram_topic_icon_state(CHAT, THREAD, custom_emoji_id="id-bug", emoji="🔥", owner="manual")
    monkeypatch.setattr("agent.title_generator.pick_topic_icon", lambda *a, **k: "📈")
    assert await runner._rename_telegram_topic_explicit(_source(), "sess-1", "Quarterly Revenue") is True
    assert "icon_custom_emoji_id" not in adapter.rename_dm_topic.await_args.kwargs
    assert db.get_telegram_topic_icon_state(CHAT, THREAD)["owner"] == "manual"


@pytest.mark.anyio
async def test_explicit_title_still_renames_when_icon_lookup_fails(tmp_path):
    runner, adapter, db = _runner(tmp_path, extra={"auto_topic_icons": True})
    adapter.get_forum_topic_icon_options = AsyncMock(side_effect=RuntimeError("telegram down"))
    assert await runner._rename_telegram_topic_explicit(_source(), "sess-1", "Quarterly Revenue") is True
    kwargs = adapter.rename_dm_topic.await_args.kwargs
    assert kwargs["name"] == "Quarterly Revenue" and "icon_custom_emoji_id" not in kwargs
    assert db.get_telegram_topic_icon_state(CHAT, THREAD) is None


@pytest.mark.anyio
async def test_explicit_title_skips_rename_when_topic_is_rebound_during_icon_pick(tmp_path, monkeypatch):
    runner, adapter, db = _runner(tmp_path, extra={"auto_topic_icons": True})
    db.create_session("replacement", source="telegram")

    def pick_and_rebind(*args, **kwargs):
        db.bind_telegram_topic(chat_id=CHAT, thread_id=THREAD, user_id=USER, session_key="k",
                               session_id="replacement")
        return "📈"

    monkeypatch.setattr("agent.title_generator.pick_topic_icon", pick_and_rebind)
    assert await runner._rename_telegram_topic_explicit(_source(), "sess-1", "Old Title") is False
    adapter.rename_dm_topic.assert_not_awaited()
    assert db.get_telegram_topic_icon_state(CHAT, THREAD) is None


@pytest.mark.anyio
async def test_explicit_title_renames_without_icon_when_pick_overruns_deadline(tmp_path, monkeypatch):
    import threading

    import gateway.run_topics as run_topics
    runner, adapter, _ = _runner(tmp_path, extra={"auto_topic_icons": True})
    release = threading.Event()
    monkeypatch.setattr(run_topics, "_EXPLICIT_TITLE_ICON_TIMEOUT_S", 0.2)
    monkeypatch.setattr("agent.title_generator.pick_topic_icon",
                        lambda *a, **k: (release.wait(5), "📈")[1])  # ignores any per-request timeout
    try:
        assert await runner._rename_telegram_topic_explicit(_source(), "sess-1", "Quarterly Revenue") is True
    finally:
        release.set()
    kwargs = adapter.rename_dm_topic.await_args.kwargs
    assert kwargs["name"] == "Quarterly Revenue" and "icon_custom_emoji_id" not in kwargs
