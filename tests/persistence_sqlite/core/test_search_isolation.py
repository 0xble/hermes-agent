"""Search failures are not misses, and writer load must not delay recall."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_state import SessionDB
from tools.session_search_tool import session_search


@pytest.fixture
def db(tmp_path):
    store = SessionDB(db_path=tmp_path / "state.db")
    store.create_session("history", source="cli", model="test")
    store.append_message("history", "user", "needle remembered")
    yield store
    store.close()


@pytest.mark.parametrize("message,is_syntax", [
    ("fts5: syntax error near \"AND\"", True),
    ("unterminated string", True),
    ("database is locked", False),
    ("database table is locked", False),
    ("interrupted", False),
    ("no such table: messages_fts", False),
])
def test_only_fts_syntax_errors_are_misses(db, monkeypatch, message, is_syntax):
    original = db._read_all

    def fail_match(sql, params=()):
        if " MATCH " in sql:
            raise sqlite3.OperationalError(message)
        return original(sql, params)

    monkeypatch.setattr(db, "_read_all", fail_match)
    if is_syntax:
        assert db.search_messages("needle") == []
    else:
        with pytest.raises(sqlite3.OperationalError, match=message):
            db.search_messages("needle")
        result = json.loads(session_search(query="needle", db=db))
        assert result["success"] is False
        assert message in result["error"]


def test_search_and_tool_hydration_bypass_writer_at_reader_capacity(db):
    # Occupy every existing reader. The search must wait for a reader to return,
    # not fall back to the writer. Keep the writer held until ALL recall finishes.
    from hermes_state import _READ_POOL_MAX

    held = [db._checkout_read_conn() for _ in range(_READ_POOL_MAX)]
    assert all(held)
    started = threading.Event()

    def recall():
        started.set()
        return db.search_messages("needle"), json.loads(session_search(query="needle", db=db))

    with ThreadPoolExecutor(max_workers=1) as executor:
        db._lock.acquire()
        try:
            future = executor.submit(recall)
            assert started.wait(5)
            db._read_pool.put_nowait(held.pop())
            rows, payload = future.result(timeout=5)
            assert len(rows) == 1 and rows[0]["context"]
            assert payload["success"] and payload["count"] == 1
            assert payload["results"][0]["messages"]
        finally:
            db._lock.release()
            for conn in held:
                db._read_pool.put_nowait(conn)


@pytest.mark.parametrize("query", ["needle", "查询历史", "记", "nevermatch needle"])
def test_match_routes_propagate_busy(db, monkeypatch, query):
    original = db._read_all

    def fail(sql, params=()):
        if " MATCH " in sql:
            raise sqlite3.OperationalError("interrupted")
        return original(sql, params)

    monkeypatch.setattr(db, "_read_all", fail)
    # Single-character CJK may choose LIKE, so force that SQL failure too.
    if query == "记":
        monkeypatch.setattr(db, "_like_rows", lambda *a, **k: fail(" MATCH "))
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        db.search_messages(query)


def test_context_errors_surface_instead_of_dropping_hits(db, monkeypatch):
    original = db._read_all

    def fail_context(sql, params=()):
        if "WITH target AS" in sql:
            raise sqlite3.OperationalError("database is locked")
        return original(sql, params)

    monkeypatch.setattr(db, "_read_all", fail_context)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        db.search_messages("needle")
    monkeypatch.setattr(db, "_read_all", original)

    def fail_view(*a, **k):
        raise sqlite3.OperationalError("interrupted")

    monkeypatch.setattr(db, "get_anchored_view", fail_view)
    payload = json.loads(session_search(query="needle", db=db))
    assert payload["success"] is False and "interrupted" in payload["error"]


@pytest.mark.parametrize("read_only", [False, True])
def test_independent_search_reader_is_read_only_without_wal_flag(db, read_only):
    from hermes_state_search_runtime import search_operation

    reader = SessionDB(db_path=db.db_path, read_only=True) if read_only else db
    reader._wal_active = False
    try:
        reader._lock.acquire()
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                assert executor.submit(reader.search_messages, "needle").result(timeout=5)
        finally:
            reader._lock.release()
        with search_operation(reader) as operation:
            assert operation.connection is not reader._conn
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                operation.connection.execute("DELETE FROM messages")
    finally:
        if read_only:
            reader.close()


def test_queue_exhaustion_is_explicit_and_cleans_up(db, monkeypatch):
    import hermes_state_search_runtime as runtime

    monkeypatch.setattr(db, "_checkout_read_conn", lambda **k: None)
    monkeypatch.setattr(runtime, "_READER_WAIT_S", 0)
    for _ in range(2):
        with pytest.raises(sqlite3.OperationalError, match="queue wait expired"):
            db.search_messages("needle")
        assert runtime._queue_for(db).waiters == []
    monkeypatch.setattr(runtime, "_MAX_WAITERS", 0)
    payload = json.loads(session_search(query="needle", db=db))
    assert payload["success"] is False and "queue full" in payload["error"]


def test_queue_prioritizes_human_over_background_fifo(db):
    from hermes_state import _READ_POOL_MAX
    from hermes_state_search_runtime import _queue_for, search_operation
    from gateway.session_context import set_session_vars, clear_session_vars
    from tests.waiting import wait_until

    held = [db._checkout_read_conn() for _ in range(_READ_POOL_MAX)]
    queue = _queue_for(db)
    order = []

    def recall(source):
        tokens = set_session_vars(source=source)
        try:
            with search_operation(db):
                order.append(source)
                assert db.search_messages("needle")
        finally:
            clear_session_vars(tokens)

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(recall, "cron")]
        try:
            wait_until(lambda: len(queue.waiters) == 1)
            futures.append(executor.submit(recall, "telegram"))
            wait_until(lambda: len(queue.waiters) == 2)
            futures.append(executor.submit(recall, "desktop"))
            wait_until(lambda: len(queue.waiters) == 3)
            db._read_pool.put_nowait(held.pop())
            for future in futures:
                future.result(timeout=5)
            assert order == ["telegram", "desktop", "cron"]
        finally:
            for conn in held:
                db._read_pool.put_nowait(conn)


def test_admission_timeout_never_interrupts_running_sql(db, monkeypatch):
    import hermes_state_search_runtime as runtime

    original = db._read_all
    executing, release = threading.Event(), threading.Event()

    def held_sql(sql, params=()):
        if " MATCH " in sql:
            executing.set()
            assert release.wait(5)
        return original(sql, params)

    monkeypatch.setattr(runtime, "_READER_WAIT_S", 0)
    monkeypatch.setattr(db, "_read_all", held_sql)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(db.search_messages, "needle")
        try:
            assert executing.wait(5)
        finally:
            release.set()
        assert len(future.result(timeout=5)) == 1


def test_stage_log_covers_tool_hydration_and_filters(db, monkeypatch, caplog):
    import logging
    import re
    import hermes_state_search

    monkeypatch.setattr(hermes_state_search, "_search_slow_ms", lambda: 0)
    with caplog.at_level(logging.INFO, logger="hermes_state"):
        result = json.loads(session_search(query="needle", db=db, sort="oldest", detail="full"))
    assert result["count"] == 1
    logs = [record.getMessage() for record in caplog.records if "slow session search:" in record.getMessage()]
    assert len(logs) == 1
    line = logs[0]
    assert "caller=session_search tool" in line and "sort=oldest" in line
    assert "role_filter" in line and "limit" in line and "path=fts5" in line
    values = {key: float(value) for key, value in re.findall(r"(\w+_ms)=(\d+\.\d+)", line)}
    assert set(values) == {"reader_queue_ms", "sql_ms", "hydration_ms", "total_ms"}
    assert values["sql_ms"] > 0
    assert sum(values[key] for key in ("reader_queue_ms", "sql_ms", "hydration_ms")) <= values["total_ms"] + .01


@pytest.mark.parametrize("sort", [None, "newest", "oldest"])
def test_pagination_with_boundary_ties_is_exact(db, sort):
    for i in range(40):
        db.append_message("history", "user", "identical tied needle", timestamp=100 + (i % 3))
    order = {None: "ORDER BY rank", "newest": "ORDER BY rank, m.timestamp DESC, m.id DESC",
             "oldest": "ORDER BY rank, m.timestamp ASC, m.id ASC"}[sort]
    sql, params = db._fts_match_sql("messages_fts", "needle", order, limit=41, offset=0,
                                    include_inactive=False, source_filter=None,
                                    exclude_sources=None, role_filter=None)
    reference = [row["id"] for row in db._read_all(sql, params)]
    actual = [row["id"] for offset in range(0, 41, 7)
              for row in db.search_messages("needle", sort=sort, limit=7, offset=offset, fields=["id"])]
    assert actual == reference


def test_corrupt_search_never_takes_writer_for_repair(db, monkeypatch):
    def broken(sql, params=()):
        raise sqlite3.DatabaseError("fts5: corrupt structure record")

    monkeypatch.setattr(db, "_read_all", broken)
    monkeypatch.setattr(db, "_enter_fts_fail_open", lambda *a, **k: pytest.fail("search attempted writer repair"))
    with pytest.raises(sqlite3.DatabaseError, match="corrupt"):
        db.search_messages("needle")


def test_sqlite_result_code_overrides_parser_looking_text():
    from hermes_state_errors import is_fts_query_syntax_error

    error = sqlite3.OperationalError('fts5: syntax error near "AND"')
    error.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT
    assert not is_fts_query_syntax_error(error)


def test_read_open_error_releases_permit_and_surfaces(db, monkeypatch):
    # Start with no idle readers so the independent opener is actually tested.
    while not db._read_pool.empty():
        db._close_read_conn(db._read_pool.get_nowait())
    available = db._read_permits._value

    def fail(*a, **k):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(db, "_connect_read_only", fail)
    payload = json.loads(session_search(query="needle", db=db))
    assert payload["success"] is False and "unable to open" in payload["error"]
    assert db._read_permits._value == available


def test_search_after_close_does_not_reopen_writer(db):
    db.close()
    payload = json.loads(session_search(query="needle", db=db))
    assert payload["success"] is False and "closed" in payload["error"]
    assert db._conn is None


def test_failed_admission_log_retains_query_and_filters(db, monkeypatch, caplog):
    import logging
    import hermes_state_search
    import hermes_state_search_runtime as runtime

    monkeypatch.setattr(runtime, "_READER_WAIT_S", 0)
    monkeypatch.setattr(db, "_checkout_read_conn", lambda **k: None)
    monkeypatch.setattr(hermes_state_search, "_search_slow_ms", lambda: 0)
    with caplog.at_level(logging.INFO, logger="hermes_state"):
        with pytest.raises(sqlite3.OperationalError, match="queue wait expired"):
            db.search_messages("needle", sort="oldest", role_filter=["user"])
    line = next(r.getMessage() for r in caplog.records if "slow session search:" in r.getMessage())
    assert "query='needle'" in line and "sort=oldest" in line
    assert "'role_filter': ['user']" in line and "path=admission" in line


