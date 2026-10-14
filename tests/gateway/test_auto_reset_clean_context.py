"""Compression exhaustion is not an explicit /new conversation boundary."""
import asyncio
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore
from hermes_state import AsyncSessionDB, SessionDB


@pytest.mark.parametrize("failure", [{"compression_exhausted": True}, {"compression_exhausted": True, "compression_deferred": True}])
def test_exhaustion_keeps_same_session_metadata_and_settings(tmp_path, failure):
    from gateway.run import GatewayRunner

    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    store._db = SessionDB(db_path=tmp_path / "state.db")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    entry = store.get_or_create_session(source)
    db = store._db
    db.create_session(entry.session_id, "telegram", user_id="u1")
    db.append_message(entry.session_id, "user", "original request sentinel")
    db.set_session_title(entry.session_id, "Runtime")
    db.set_session_title_source(entry.session_id, "user")
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._session_db = AsyncSessionDB(db)
    runner._evict_cached_agent = MagicMock()
    runner._clear_conversation_scope = MagicMock()
    runner._sync_telegram_topic_binding = MagicMock()
    response, returned = asyncio.run(runner._hmwa_compression_exhaustion_reset(
        failure, "Recovery paused. Input is retained.", entry, entry.session_key, source))
    assert returned is entry
    assert store.get_or_create_session(source).session_id == entry.session_id
    assert db.get_session_title(entry.session_id) == "Runtime"
    assert db.get_session_title_source(entry.session_id) == "user"
    assert store.load_transcript(entry.session_id)[0]["content"] == "original request sentinel"
    runner._clear_conversation_scope.assert_not_called()
    runner._evict_cached_agent.assert_not_called()
    runner._sync_telegram_topic_binding.assert_not_called()
    assert "retained" in response and "fresh" not in response


def test_explicit_reset_still_creates_fresh_conversation(tmp_path):
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    store._db = SessionDB(db_path=tmp_path / "state.db")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    entry = store.get_or_create_session(source)
    store._db.create_session(entry.session_id, "telegram")
    store._db.append_message(entry.session_id, "user", "retained old history")
    replacement = store.reset_session(entry.session_key)
    assert replacement.session_id != entry.session_id
    assert store.load_transcript(replacement.session_id) == []
    assert store.load_transcript(entry.session_id)[0]["content"] == "retained old history"
