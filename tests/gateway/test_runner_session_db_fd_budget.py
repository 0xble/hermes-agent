"""Single-SessionDB ownership for the gateway runner.

Upstream now owns the bounded pooled-reader lifecycle.  These fork regressions
cover only the remaining private contract: ``GatewayRunner`` must not construct
a second ``SessionDB`` beside the one ``SessionStore`` already owns.
"""

from hermes_state import SessionDB


# ── Gateway ownership ──


def test_runner_reuses_session_store_db(tmp_path):
    """The runner's resolver returns the store's instance, opening nothing new."""
    import hermes_state
    from gateway.run import GatewayRunner
    from hermes_state import AsyncSessionDB

    store_db = SessionDB(tmp_path / "state.db")
    store = type("S", (), {"_db": store_db})()

    opened = []
    real_init = hermes_state.SessionDB.__init__

    def counting_init(self, db_path=None, read_only=False):
        opened.append(self)
        return real_init(self, db_path, read_only)

    hermes_state.SessionDB.__init__ = counting_init
    try:
        shared = GatewayRunner._resolve_shared_session_db(store)
        session_db = AsyncSessionDB(shared)

        # Object identity, not merely constructor count: these three handles
        # must be the same synchronous database.
        assert shared is store_db
        assert session_db._db is store_db
        assert store._db is store_db
        assert opened == [], "a second SessionDB was constructed"
    finally:
        hermes_state.SessionDB.__init__ = real_init
        store_db.close()


def test_runner_replacement_db_is_stored_back_on_the_store(tmp_path, monkeypatch):
    """When SessionStore has no DB, the runner's replacement becomes the store's."""
    import hermes_state
    from gateway.run import GatewayRunner
    from hermes_state import AsyncSessionDB

    monkeypatch.setattr(
        hermes_state, "_default_db_path", lambda: tmp_path / "state.db"
    )

    store = type("S", (), {"_db": None})()
    shared = GatewayRunner._resolve_shared_session_db(store)
    session_db = AsyncSessionDB(shared)

    try:
        assert shared is not None
        assert store._db is shared, "replacement was not stored back on the store"
        assert session_db._db is store._db
    finally:
        shared.close()


def test_shutdown_closes_shared_db_once(tmp_path, monkeypatch):
    """Identity dedup means one close() call even though two handles point at it."""
    import hermes_state
    from gateway.run import GatewayRunner
    from hermes_state import AsyncSessionDB

    monkeypatch.setattr(
        hermes_state, "_default_db_path", lambda: tmp_path / "state.db"
    )
    store = type("S", (), {"_db": None})()
    shared = GatewayRunner._resolve_shared_session_db(store)
    async_db = AsyncSessionDB(shared)

    calls = []
    real_close = shared.close
    monkeypatch.setattr(
        shared, "close", lambda: (calls.append(1), real_close())[1]
    )

    # Mirrors the runner's shutdown loop.
    self_db = getattr(async_db, "_db", async_db)
    seen: set = set()
    for db_obj in (self_db, getattr(store, "_db", None)):
        if db_obj is None or not hasattr(db_obj, "close"):
            continue
        if id(db_obj) in seen:
            continue
        seen.add(id(db_obj))
        db_obj.close()

    assert calls == [1], "shared SessionDB was closed more than once"
