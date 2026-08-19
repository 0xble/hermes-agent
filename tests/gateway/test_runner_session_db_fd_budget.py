"""Single-SessionDB ownership for the gateway runner.

Upstream now owns the bounded pooled-reader lifecycle.  These fork regressions
cover only the remaining private contract: ``GatewayRunner`` must not construct
a second ``SessionDB`` beside the one ``SessionStore`` already owns.
"""

import pytest

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


def test_runner_preserves_explicit_degraded_store_without_replacement(monkeypatch):
    """A deliberate JSONL/degraded state must not reopen SQLite."""
    import hermes_state
    from gateway.run import GatewayRunner

    def forbidden_session_db(*_args, **_kwargs):
        raise AssertionError("runner must not manufacture a replacement SessionDB")

    monkeypatch.setattr(hermes_state, "SessionDB", forbidden_session_db)
    store = type("S", (), {"_db": None})()

    with pytest.raises(RuntimeError, match="no SQLite handle"):
        GatewayRunner._resolve_shared_session_db(store)

    assert getattr(store, "_db") is None
