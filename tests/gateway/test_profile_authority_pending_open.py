"""A profile rejoin must join a pending store open, not park a healthy authority."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig
from gateway.run import GatewayRunner
from gateway.run_runtime import _build_profile_authority
from gateway.session import SessionStore
from gateway.session_authorities import SessionAuthorities


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_cache", ["store", "runner"])
async def test_profile_rejoin_joins_the_existing_store_open(tmp_path, monkeypatch, pending_cache):
    import hermes_state
    import hermes_state_registry as registry

    home = (tmp_path / "profiles" / "satellite").resolve()
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    with patch.object(SessionStore, "_ensure_loaded"):
        store = SessionStore(sessions_dir=home / "sessions", config=GatewayConfig())
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._init_session_db()
    runner.session_authorities = SessionAuthorities(home, multiplexed=True)
    runner.session_runtime_descriptor = {"instance_id": "restored-owner"}
    first = store._db
    assert runner._session_db._db is first
    assert registry.close_all_under(home) == 1  # the snapshot handoff's unserve

    entered, release, joining = threading.Event(), threading.Event(), threading.Event()
    acquire = registry.acquire

    def pending_acquire(path):
        entered.set()
        assert release.wait(10), "the test must release the existing open"
        return acquire(path)

    monkeypatch.setattr(registry, "acquire", pending_acquire)
    cache = store._db_handle_cache
    get = cache.get

    def observe_join(path, opener, **kwargs):
        with cache.lock:
            slot = cache._unavailable.get(path)
            if slot is not None and slot.in_flight:
                joining.set()
        return get(path, opener, **kwargs)

    monkeypatch.setattr(cache, "get", observe_join)

    async def initialize(_runner, *, db, **kwargs):
        return SimpleNamespace(db=db)

    monkeypatch.setattr("gateway.session_authority.initialize_session_authority", initialize)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            opening = pool.submit(lambda: store._db if pending_cache == "store" else runner._session_db)
            assert await asyncio.to_thread(entered.wait, 10)
            rejoin = asyncio.create_task(_build_profile_authority(runner, "satellite", home, register=False))
            try:
                assert await asyncio.to_thread(joining.wait, 10), "rejoin never encountered the pending open"
            finally:
                release.set()
            fresh = await asyncio.wrap_future(opening)
            authority = await rejoin
        db = getattr(fresh, "_db", fresh)
        assert authority.db is db and db is not first
        assert runner._session_db._db is db is store._db
        assert not runner._session_db_handle_cache.last_error(home / "state.db")
        shared = registry.acquire(home / "state.db")
        try:
            assert shared is db, "rejoin must not open a second writer"
        finally:
            registry.release(shared)
    finally:
        release.set()
        store.close_all_db_handles()
        runner.close_all_session_db_handles()
