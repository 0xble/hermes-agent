import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run_turn import GatewayTurnMixin
from gateway.session import SessionSource, SessionStore


@pytest.fixture
def private_store(tmp_path):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", user_id="user", thread_id="topic")
    entry = store.get_or_create_session(source)
    yield store, entry
    # The private registry is process-wide, so leave the topic public for whatever runs next.
    if store.incognito_enabled(entry.session_key):
        store.set_incognito(entry.session_key, False)
    store._db.close()


def _runner(store):
    runner = object.__new__(GatewayTurnMixin)
    runner.session_store = store
    return runner


def test_restored_private_sidecar_gets_restart_notice_once(private_store):
    store, entry = private_store
    store.set_incognito(entry.session_key, True)
    restored = SessionStore(store.sessions_dir, GatewayConfig())
    restored_entry = restored.lookup_by_session_key(entry.session_key)
    runner = _runner(restored)

    first = runner._prepend_incognito_restart_notice(restored_entry, "reply after restart")
    second = runner._prepend_incognito_restart_notice(restored_entry, "second reply")

    assert first == (
        "Hermes restarted, so the private messages before this were dropped. "
        "Incognito is still on.\nreply after restart"
    )
    assert second == "second reply"
    restored._db.close()


def test_private_toggle_does_not_get_restart_notice(private_store):
    store, entry = private_store
    store.set_incognito(entry.session_key, True)
    runner = _runner(store)

    assert runner._prepend_incognito_restart_notice(entry, "private reply") == "private reply"


def test_rotated_private_session_does_not_get_restart_notice(private_store):
    store, entry = private_store
    store.set_incognito(entry.session_key, True)
    entry.session_id = "rotated-session"
    runner = _runner(store)

    assert runner._prepend_incognito_restart_notice(entry, "private reply") == "private reply"


def test_public_session_does_not_get_restart_notice(private_store):
    store, entry = private_store
    runner = _runner(store)

    assert runner._prepend_incognito_restart_notice(entry, "public reply") == "public reply"


def test_restart_notice_clears_when_incognito_turns_off(private_store):
    store, entry = private_store
    store.set_incognito(entry.session_key, True)
    restored = SessionStore(store.sessions_dir, GatewayConfig())
    restored_entry = restored.lookup_by_session_key(entry.session_key)
    runner = _runner(restored)
    restored.set_incognito(restored_entry.session_key, False)

    public_reply = runner._prepend_incognito_restart_notice(restored_entry, "public after off")

    assert public_reply == "public after off"
    assert "still on" not in public_reply
    restored._db.close()
