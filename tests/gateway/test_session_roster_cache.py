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


def test_roster_invalidates_when_route_is_created_after_snapshot(tmp_path):
    store = SessionStore(tmp_path / "sessions", GatewayConfig(write_sessions_json=False))
    store.get_or_create_session(_source("one"))
    assert len(store.list_sessions()) == 1
    generation, _ = store.list_sessions_since()
    created = store.get_or_create_session(_source("two"))
    assert len(store.list_sessions()) == 2
    changed_generation, changed = store.list_sessions_since(generation)
    assert changed_generation != generation
    assert changed is not None
    assert {item.session_key for item in changed} == {
        store._generate_session_key(_source("one")), store._generate_session_key(_source("two"))
    }
    assert created.session_key == store._generate_session_key(_source("two"))


def test_roster_invalidates_for_reset_and_recovery(tmp_path):
    store = SessionStore(tmp_path / "sessions", GatewayConfig(write_sessions_json=False))
    entry = store.get_or_create_session(_source("one"))
    generation, _ = store.list_sessions_since()
    reset = store.reset_session(entry.session_key)
    changed_generation, changed = store.list_sessions_since(generation)
    assert reset.session_id != entry.session_id
    assert changed_generation != generation
    assert changed is not None
    assert changed[0].session_id == reset.session_id

    generation, _ = store.list_sessions_since()
    store._entries.pop(entry.session_key)
    recovered = store.get_or_create_session(_source("one"))
    changed_generation, changed = store.list_sessions_since(generation)
    assert recovered.session_id == reset.session_id
    assert changed_generation != generation
    assert changed is not None
    assert changed[0].session_id == recovered.session_id


def test_roster_invalidates_when_runtime_sessions_are_retired(tmp_path):
    store = SessionStore(tmp_path / "sessions", GatewayConfig(write_sessions_json=False))
    entry = store.get_or_create_session(_source("one"))
    generation, _ = store.list_sessions_since()
    store.retire_runtime_sessions([entry.session_id])
    changed_generation, changed = store.list_sessions_since(generation)
    assert changed_generation != generation
    assert changed is not None
    assert changed == []


def test_roster_invalidates_when_model_role_replaces_entry(tmp_path):
    store = SessionStore(tmp_path / "sessions", GatewayConfig(write_sessions_json=False))
    entry = store.get_or_create_session(_source("one"))
    generation, _ = store.list_sessions_since()
    assert store.set_model_roles([entry.session_key], "fast") == [entry.session_key]
    changed_generation, changed = store.list_sessions_since(generation)
    assert changed_generation != generation
    assert changed is not None
    assert changed[0].model_role == "fast"


def test_direct_entries_assignment_invalidates_roster(tmp_path):
    store = SessionStore(tmp_path / "sessions", GatewayConfig(write_sessions_json=False))
    entry = store.get_or_create_session(_source("one"))
    generation, _ = store.list_sessions_since()
    replacement = dict(store._entries)
    replacement["telegram:two"] = entry
    store._entries = replacement
    changed_generation, changed = store.list_sessions_since(generation)
    assert changed_generation != generation
    assert changed is not None
    assert len(changed) == 2


def test_session_entry_callback_is_not_a_strong_store_reference(tmp_path):
    import gc
    import pickle
    import weakref

    store = SessionStore(tmp_path / "sessions", GatewayConfig(write_sessions_json=False))
    entry = store.get_or_create_session(_source("one"))
    assert "_roster_change" not in entry.to_dict()
    # This round-trip is over an entry created in this test, not untrusted input; it verifies
    # the non-serializable weak callback is excluded from the entry wire state.
    assert pickle.loads(pickle.dumps(entry))._roster_change is None
    store_ref = weakref.ref(store)
    del store
    gc.collect()
    assert store_ref() is None
