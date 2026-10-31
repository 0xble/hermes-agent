"""Gateway privacy boundaries use real stores and disposable Hermes homes."""
import json
import secrets

import pytest
from gateway.run import GatewayRunner  # entrypoint bootstrap runs at collection, before the home guard
from hermes_state import SessionDB


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr("gateway.session_persistence.SessionPersistenceMixin._open_session_db_for_active_scope",
                        lambda self, db_path=None: db)
    yield
    db.close()

from agent.incognito import incognito_scope, register_session
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore


@pytest.mark.parametrize("metadata_bom", [False, True])
def test_private_interval_is_volatile_and_restart_keeps_only_prefix(tmp_path, metadata_bom):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="17", thread_id="23", user_id="4")
    entry = store.get_or_create_session(source)
    sid, key = entry.session_id, entry.session_key
    prefix = [{"role": "user", "content": "saved prefix"}, {"role": "assistant", "content": "saved reply"}]
    for msg in prefix:
        store.append_to_transcript(sid, msg)
    store.set_incognito(key, True)
    if metadata_bom:
        sidecar = store.sessions_dir / "incognito.json"
        sidecar.write_bytes(b"\xef\xbb\xbf" + sidecar.read_bytes())
    from types import SimpleNamespace
    from agent.incognito import is_incognito
    from gateway.incognito import event_scope
    adapter = SimpleNamespace(_source_session_key=lambda source: key)
    with event_scope(adapter, SimpleNamespace(source=source)):
        assert is_incognito()
    canary = secrets.token_hex(24)
    with incognito_scope(True, sid):
        for i in range(2):
            for role in ("user", "assistant", "tool", "assistant"):
                store.append_to_transcript(sid, {"role": role, "content": canary + str(i)}, skip_db=True)
        assert canary in json.dumps(store.load_transcript(sid))
        entry.display_name = canary
        store.update_session(key)
        assert store.get_or_create_session(source).session_id == sid
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert canary.encode() not in path.read_bytes(), path
    restarted = SessionStore(tmp_path / "sessions", GatewayConfig())
    restored = restarted.lookup_by_session_key(key)
    assert restored.incognito
    assert [m["content"] for m in restarted.load_transcript(sid)] == [m["content"] for m in prefix]
    assert restarted.get_or_create_session(source).session_id == sid
    restarted.set_incognito(key, False)
    restarted.append_to_transcript(sid, {"role": "user", "content": "after off"})
    assert [m["content"] for m in restarted.load_transcript(sid)] == ["saved prefix", "saved reply", "after off"]
    register_session(sid, False)
    register_session(key, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_async_ingress_scopes_real_store_without_blocking_gateway_loop(tmp_path, private):
    import asyncio
    import threading
    from types import SimpleNamespace

    from agent.incognito import is_incognito
    from gateway.incognito import private_ingress, private_topic
    from hermes_loop_guard import set_loop_thread

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="loop-ingress", thread_id="23")
    entry = store.get_or_create_session(source)
    key = entry.session_key
    if private:
        store.set_incognito(key, True)
    owner = SimpleNamespace(session_store=store, _session_key_for_source=lambda source: key)
    seen = []

    @private_ingress
    async def receive(self, event):
        await asyncio.sleep(0)
        seen.append((threading.get_ident(), is_incognito(), private_topic()))
        assert await asyncio.to_thread(is_incognito) is private

    loop_thread = threading.get_ident()
    set_loop_thread(loop_thread)
    try:
        await receive(owner, SimpleNamespace(source=source))
        assert seen == [(loop_thread, private, key)]
        assert not is_incognito() and private_topic() is None
    finally:
        set_loop_thread(None)
        register_session(key, False)
        register_session(entry.session_id, False)


@pytest.mark.asyncio
async def test_async_ingress_uses_executor_only_until_routing_index_loads(tmp_path, monkeypatch):
    """A restarted store must load its flag off-loop; afterward ingress never queues on the executor."""
    import asyncio
    import threading
    from types import SimpleNamespace

    from agent.incognito import is_incognito
    from gateway.incognito import event_scope
    from hermes_loop_guard import set_loop_thread

    sessions = tmp_path / "sessions"
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="index-load", thread_id="7")
    entry = SessionStore(sessions, GatewayConfig()).get_or_create_session(source)
    key = entry.session_key
    SessionStore(sessions, GatewayConfig()).set_incognito(key, True)
    # A restart keeps only the durable flag, not the in-process registry.
    register_session(key, False)
    register_session(entry.session_id, False)
    restarted = SessionStore(sessions, GatewayConfig())
    owner = SimpleNamespace(session_store=restarted, _session_key_for_source=lambda source: key)
    hops = []
    real_to_thread = asyncio.to_thread

    async def counting_to_thread(fn, *args, **kwargs):
        hops.append(fn)
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", counting_to_thread)
    set_loop_thread(threading.get_ident())
    try:
        for _ in range(2):
            async with event_scope(owner, SimpleNamespace(source=source)):
                assert is_incognito()
        assert len(hops) == 1
    finally:
        set_loop_thread(None)
        register_session(key, False)
        register_session(entry.session_id, False)


def test_new_drops_private_history_and_starts_normal(tmp_path):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    entry = store.get_or_create_session(SessionSource(platform=Platform.TELEGRAM, chat_id="18"))
    store.set_incognito(entry.session_key, True)
    with incognito_scope(True, entry.session_id):
        store.append_to_transcript(entry.session_id, {"role": "user", "content": "discarded"})
    with incognito_scope(True, entry.session_id):
        new = store.reset_session(entry.session_key)
    assert new.session_id != entry.session_id
    assert not new.incognito
    assert not store.incognito_enabled(entry.session_key)
    store.append_to_transcript(new.session_id, {"role": "user", "content": "normal"})
    assert store.load_transcript(new.session_id)[0]["content"] == "normal"
