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


def test_stale_boundary_refuses_only_its_own_topic(tmp_path):
    """One record that no longer matches its route must not take down every topic."""
    from agent.incognito import is_incognito

    sessions = tmp_path / "sessions"
    store = SessionStore(sessions, GatewayConfig())
    private = store.get_or_create_session(SessionSource(platform=Platform.TELEGRAM, chat_id="stale", thread_id="1"))
    public = store.get_or_create_session(SessionSource(platform=Platform.TELEGRAM, chat_id="fine", thread_id="2"))
    store.set_incognito(private.session_key, True)
    path = sessions / "incognito.json"
    records = json.loads(path.read_text())
    records[private.session_key]["session_id"] = "20261030_000000_deadbeef"
    path.write_text(json.dumps(records))
    register_session(private.session_key, False)
    register_session(private.session_id, False)
    try:
        restarted = SessionStore(sessions, GatewayConfig())
        assert restarted.incognito_enabled(public.session_key) is False
        assert restarted.lookup_by_session_key(public.session_key).session_id == public.session_id
        assert restarted.published_incognito_enabled(public.session_key) is False
        with pytest.raises(ValueError):
            restarted.incognito_enabled(private.session_key)
        assert restarted.published_incognito_enabled(private.session_key) is None
        assert is_incognito("20261030_000000_deadbeef")  # the recorded identity stays private
    finally:
        register_session("20261030_000000_deadbeef", False)
        register_session(private.session_key, False)
        register_session(private.session_id, False)


@pytest.mark.asyncio
async def test_adapter_ingress_uses_its_store_on_the_loop_without_reading_routing_files(tmp_path, monkeypatch):
    """Adapters hold the runner store as ``_session_store``; sync and async ingress stay on-loop."""
    import threading
    from types import SimpleNamespace

    from agent.incognito import is_incognito
    import gateway.incognito as gateway_incognito
    from hermes_loop_guard import set_loop_thread

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="adapter-store", thread_id="5")
    entry = store.get_or_create_session(source)
    key = entry.session_key
    store.set_incognito(key, True)
    adapter = SimpleNamespace(_session_store=store, _source_session_key=lambda source: key)
    monkeypatch.setattr(gateway_incognito, "_routing_records",
                        lambda: (_ for _ in ()).throw(AssertionError("routing file read on ingress")))
    seen = []

    @gateway_incognito.private_ingress
    def enqueue(self, event):
        seen.append(is_incognito())

    @gateway_incognito.private_ingress
    async def receive(self, event):
        seen.append(is_incognito())

    set_loop_thread(threading.get_ident())
    try:
        enqueue(adapter, SimpleNamespace(source=source))
        await receive(adapter, SimpleNamespace(source=source))
        assert seen == [True, True]
    finally:
        set_loop_thread(None)
        register_session(key, False)
        register_session(entry.session_id, False)


def test_private_caller_scope_does_not_count_as_a_registered_boundary(tmp_path, monkeypatch):
    """A routing record still registers its topic when the caller is already private."""
    from types import SimpleNamespace

    from agent.incognito import is_registered
    import gateway.incognito as gateway_incognito

    key, session_id = "agent:main:telegram:dm:records:1", "20261030_000000_records"
    monkeypatch.setattr(gateway_incognito, "_routing_records", lambda: {key: {"session_id": session_id}})
    owner = SimpleNamespace(_source_session_key=lambda source: key)
    event = SimpleNamespace(source=SimpleNamespace())
    try:
        with incognito_scope(True, "some-other-private-topic"):
            assert gateway_incognito._event_decision(owner, event, blocking=False) is gateway_incognito._OFFLOAD
            assert gateway_incognito._event_decision(owner, event) == (key, True)
        assert is_registered(key) and is_registered(session_id)
    finally:
        register_session(key, False)
        register_session(session_id, False)


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
