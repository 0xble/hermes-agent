"""Focused tests for the routed-session roster generation cache."""

import copy
import json

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


def test_roster_generation_changes_after_route_mutation(tmp_path):
    store = SessionStore(tmp_path / "sessions", GatewayConfig(write_sessions_json=False))
    entry = store.get_or_create_session(_source("one"))
    generation, _ = store.list_sessions_since()
    assert store.unbind_session(entry.session_key, entry.session_id)
    changed_generation, changed = store.list_sessions_since(generation)
    assert changed_generation != generation
    assert changed == []


def test_external_sessions_json_route_is_seen_after_signature_change(tmp_path):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, GatewayConfig(write_sessions_json=True))
    entry = store.get_or_create_session(_source("one"))
    generation, _ = store.list_sessions_since()
    path = sessions_dir / "sessions.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    external = copy.deepcopy(entry.to_dict())
    external["session_id"] = "external-session"
    document["external-route"] = external
    path.write_text(json.dumps(document), encoding="utf-8")
    changed_generation, changed = store.list_sessions_since(generation)
    assert changed is not None
    assert changed_generation != generation
    assert {item.session_key for item in changed} == {entry.session_key, "external-route"}
