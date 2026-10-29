"""A session's /reasoning pick must survive a gateway restart, like /model overrides do.

Before persistence the pick lived only in ``SessionState.conversation``, so a restart silently
dropped every session back to ``agent.reasoning_effort``.
"""
import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore

XHIGH = {"enabled": True, "effort": "xhigh"}


@pytest.fixture
def store_factory(tmp_path, monkeypatch):
    import hermes_state

    def _raise():
        raise RuntimeError("SQLite disabled in test")

    monkeypatch.setattr(hermes_state, "SessionDB", _raise)
    return lambda: SessionStore(sessions_dir=tmp_path, config=GatewayConfig())


def _runner(store):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._session_reasoning_overrides = {}
    runner.session_store = store
    runner._load_reasoning_config = lambda model="": {"enabled": True, "effort": "high"}
    return runner


def _source():
    return SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="c1", chat_type="dm", thread_id="296363")


def test_reasoning_pick_survives_restart_and_reset_clears_it(store_factory):
    store = store_factory()
    key = store.get_or_create_session(_source()).session_key
    _runner(store)._set_session_reasoning_override(key, XHIGH)

    # Simulated restart: fresh store over the same sessions dir, fresh runner with empty memory.
    restarted = _runner(store_factory())
    assert restarted._resolve_session_reasoning_config(session_key=key) == XHIGH

    # /new starts a fresh route: the pick must not resurrect after another restart.
    restarted.session_store.reset_session(key)
    assert _runner(store_factory())._resolve_session_reasoning_config(session_key=key) == {
        "enabled": True, "effort": "high"}


def test_reasoning_reset_clears_the_persisted_pick(store_factory):
    store = store_factory()
    key = store.get_or_create_session(_source()).session_key
    runner = _runner(store)
    runner._set_session_reasoning_override(key, XHIGH)
    runner._set_session_reasoning_override(key, None)
    assert _runner(store_factory())._resolve_session_reasoning_config(session_key=key) == {
        "enabled": True, "effort": "high"}


@pytest.mark.parametrize("raw, expected", [
    ({"enabled": True, "effort": "xhigh", "api_key": "sk-leak"}, XHIGH),
    ({"enabled": False}, {"enabled": False}),
    ("xhigh", None),
    ({"enabled": True}, None),
])
def test_persisted_reasoning_pick_is_sanitized_on_load(raw, expected):
    from datetime import datetime

    from gateway.session import SessionEntry

    now = datetime.now().isoformat()
    entry = SessionEntry.from_dict({
        "session_key": "k", "session_id": "s", "created_at": now, "updated_at": now,
        "reasoning_override": raw,
    })
    assert entry.reasoning_override == expected
    assert entry.to_dict().get("reasoning_override") == expected
