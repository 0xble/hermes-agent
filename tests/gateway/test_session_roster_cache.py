"""Focused tests for the routed-session roster generation cache."""

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore


def _source(chat_id):
    return SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, user_id=chat_id)


def test_roster_generation_reuses_snapshot_and_returns_copies(tmp_path):
    store = SessionStore(tmp_path / "sessions", GatewayConfig(write_sessions_json=False))
    entry = store.get_or_create_session(_source("one"))
    generation, entries = store.list_sessions_since()
    assert entries is not None
    assert [item.session_key for item in entries] == [entry.session_key]
    entries.clear()
    same_generation, unchanged = store.list_sessions_since(generation)
    assert same_generation == generation
    assert unchanged is None
    assert store.list_sessions() == [entry]


def test_roster_generation_invalidates_on_entry_field_mutation(tmp_path):
    store = SessionStore(tmp_path / "sessions", GatewayConfig(write_sessions_json=False))
    entry = store.get_or_create_session(_source("one"))
    generation, _ = store.list_sessions_since()
    entry.suspended = True
    changed_generation, changed = store.list_sessions_since(generation)
    assert changed_generation != generation
    assert changed is not None
    assert changed[0].suspended is True
