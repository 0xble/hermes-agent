"""Reader budget and single-SessionDB ownership for the gateway runner.

Two related descriptor problems are covered here:

* ``SessionDB`` opened one read-only WAL connection per thread with no
  ceiling, so a long-running gateway accumulated hundreds of ``state.db``
  descriptors (289 observed against a launchd soft limit of 256).
* ``GatewayRunner`` constructed a *second* ``SessionDB`` beside the one
  ``SessionStore`` had already opened, doubling both writer connections and
  reader pools.

Descriptor assertions go through Hermes's tracked-connection registry rather
than ``lsof`` so they are deterministic and portable; an ``lsof`` probe is an
operational check, not a unit test.
"""

import threading

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    database.create_session("s1", source="test")
    if not database._wal_active:
        database.close()
        pytest.skip("host SQLite did not enable WAL; read-path split inactive")
    try:
        yield database
    finally:
        database.close()


# ── Reader budget ──


def test_reader_count_never_exceeds_budget(db):
    """More concurrent threads than the budget still yields at most the budget."""
    budget = SessionDB._MAX_READ_CONNECTIONS
    n_threads = budget * 4
    assert n_threads > budget

    start = threading.Barrier(n_threads)
    hold = threading.Event()
    peaks = []
    errors = []

    def worker():
        try:
            # All threads arrive together so the pre-check and the
            # post-open re-check are genuinely contended.
            start.wait(timeout=30)
            # An existing session with no handoff record yields a dict of
            # NULLs; only a missing session yields None.
            assert db.get_handoff_state("s1") == {
                "state": None, "platform": None, "error": None,
            }
            peaks.append(len(db._read_conns))
            # Stay alive so every thread's reader is simultaneously live —
            # otherwise finished threads would let later ones under the cap.
            hold.wait(timeout=30)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    # Let every worker reach the hold before sampling the peak.
    deadline = threading.Event()
    for _ in range(300):
        if len(peaks) >= n_threads or errors:
            break
        deadline.wait(0.05)

    peak = len(db._read_conns)
    hold.set()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    assert errors == [], f"reads failed under budget pressure: {errors}"
    assert peak <= budget, f"opened {peak} readers, budget is {budget}"
    assert all(p <= budget for p in peaks), f"observed over-budget peaks: {max(peaks)}"


def test_threads_over_budget_still_read_successfully(db):
    """Excess threads fall back to the locked writer path, not to an error."""
    budget = SessionDB._MAX_READ_CONNECTIONS
    results = []
    errors = []
    hold = threading.Event()
    saturated = threading.Barrier(budget + 1)

    def saturator():
        try:
            assert db._get_read_conn() is not None
            saturated.wait(timeout=30)
            hold.wait(timeout=30)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    holders = [threading.Thread(target=saturator) for _ in range(budget)]
    for t in holders:
        t.start()
    saturated.wait(timeout=30)
    assert len(db._read_conns) == budget

    def latecomer():
        try:
            # No reader slot available: must fall back, not fail.
            assert db._get_read_conn() is None
            results.append(db.list_pending_handoffs())
            results.append(db.get_compression_lock_holder("s1"))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    late = [threading.Thread(target=latecomer) for _ in range(8)]
    for t in late:
        t.start()
    for t in late:
        t.join(timeout=30)
        assert not t.is_alive()

    hold.set()
    for t in holders:
        t.join(timeout=30)

    assert errors == [], f"fallback reads failed: {errors}"
    assert len(results) == 16
    assert len(db._read_conns) == budget, "fallback path registered extra readers"


def test_budget_pressure_does_not_permanently_demote_a_thread(db):
    """A thread that loses the budget race upgrades once a slot frees.

    Budget exhaustion is transient; a failed *open* is not. Conflating them
    would leave a long-lived gateway worker on the locked fallback forever
    because it lost one race at startup.
    """
    budget = SessionDB._MAX_READ_CONNECTIONS
    hold = threading.Event()
    saturated = threading.Barrier(budget + 1)
    released = threading.Event()

    def saturator():
        db._get_read_conn()
        saturated.wait(timeout=30)
        hold.wait(timeout=30)

    holders = [threading.Thread(target=saturator) for _ in range(budget)]
    for t in holders:
        t.start()
    saturated.wait(timeout=30)

    # This thread is shut out right now...
    assert db._get_read_conn() is None
    assert not getattr(db._read_local, "failed", False), "thread was marked failed"

    # ...but must recover once the holders exit, with no external help:
    # _get_read_conn reclaims slots pinned by dead threads.
    hold.set()
    for t in holders:
        t.join(timeout=30)

    released.set()
    assert db._get_read_conn() is not None, "thread never regained a reader slot"
    assert len(db._read_conns) <= budget


def test_churning_short_lived_threads_do_not_exhaust_the_budget(db):
    """Long-run shape: many sequential short-lived readers stay under budget.

    Each thread exits before the next starts, so without reclamation the
    first _MAX_READ_CONNECTIONS threads would pin every slot permanently and
    every later read would silently convoy on the writer lock.
    """
    budget = SessionDB._MAX_READ_CONNECTIONS
    got_reader = []

    for _ in range(budget * 5):
        def worker():
            got_reader.append(db._get_read_conn() is not None)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=30)
        assert not t.is_alive()

    assert len(db._read_conns) <= budget, "reader set grew past the budget"
    # Reclamation means later threads still get real readers, not fallbacks.
    assert got_reader[-1] is True, "budget ratcheted shut against new threads"
    assert sum(got_reader) == len(got_reader), (
        f"only {sum(got_reader)}/{len(got_reader)} threads got a reader"
    )


def test_live_thread_readers_are_never_reclaimed(db):
    """Reclamation must only take slots from threads that have exited."""
    budget = SessionDB._MAX_READ_CONNECTIONS
    hold = threading.Event()
    ready = threading.Barrier(3)
    conns = []

    def holder():
        conns.append(db._get_read_conn())
        ready.wait(timeout=30)
        hold.wait(timeout=30)

    holders = [threading.Thread(target=holder) for _ in range(2)]
    for t in holders:
        t.start()
    ready.wait(timeout=30)

    with db._read_conns_lock:
        reclaimed = db._reclaim_dead_readers_locked()

    assert reclaimed == 0, "reclaimed a reader owned by a live thread"
    assert all(c in db._read_conns for c in conns)
    # And they are still usable by their owners.
    for c in conns:
        assert c.execute("SELECT 1").fetchone()[0] == 1

    hold.set()
    for t in holders:
        t.join(timeout=30)
    assert budget >= len(conns)


def test_close_drains_readers_created_by_worker_threads(tmp_path):
    """close() releases every tracked reader, including other threads'."""
    database = SessionDB(tmp_path / "state.db")
    database.create_session("s1", source="test")
    if not database._wal_active:
        database.close()
        pytest.skip("host SQLite did not enable WAL")

    done = threading.Barrier(9)

    def worker():
        database.get_handoff_state("s1")
        done.wait(timeout=30)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    done.wait(timeout=30)
    for t in threads:
        t.join(timeout=30)

    assert len(database._read_conns) == 8
    database.close()
    assert database._read_conns == set()


def test_close_is_idempotent(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    database.create_session("s1", source="test")
    database.close()
    database.close()  # must not raise
    assert database._read_conns == set()


def test_no_reader_registers_after_close(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    database.create_session("s1", source="test")
    wal = database._wal_active
    database.close()
    if not wal:
        pytest.skip("host SQLite did not enable WAL")

    results = []

    def worker():
        results.append(database._get_read_conn())

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=30)

    assert results == [None]
    assert database._read_conns == set()


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
