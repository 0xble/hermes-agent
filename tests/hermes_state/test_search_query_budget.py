"""Broad recall must fail explicitly, without losing full-body tool-tail recall."""

import json
import logging
import sqlite3
from contextlib import contextmanager

import pytest

from hermes_state import SessionDB
from tools.session_search_tool import session_search


@pytest.fixture
def corpus(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("history", source="cli", model="test")
    # Common identifier tokens and sparse matches force more than one progress interval.
    db._conn.executemany(
        "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        [("history", "tool", f"proc common recovery_context fixture {i}", i) for i in range(4000)],
    )
    db._conn.commit()
    yield db
    db.close()


@pytest.mark.parametrize("query,roles,detail", [
    ("proc_58b8d12db05f", "tool", "full"),
    ('"deleg_def0dbfd" "recovery_context"', "tool", "adaptive"),
    ('"fix/resume-admission-fixture"', "assistant,tool", "adaptive"),
    ("common", "tool", "full"),
    ("common", "user,assistant", "adaptive"),
])
def test_broad_query_is_an_error_and_reader_is_reusable(corpus, monkeypatch, query, roles, detail):
    import hermes_state_search_runtime as runtime

    clock = [runtime.time.monotonic()]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    original = corpus._read_all
    statements = []

    def expire_at_search(sql, params=()):
        if (" LIKE " in sql and "FROM messages m" in sql) or " MATCH " in sql:
            statements.append(sql)
            clock[0] += 31.0
        return original(sql, params)

    monkeypatch.setattr(corpus, "_read_all", expire_at_search)
    returned = []
    original_return = corpus._return_read_conn

    def check_return(conn):
        # This exact recycled connection must have its interrupt callback removed.
        assert conn.execute(
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<2000) "
            "SELECT sum(x) FROM n"
        ).fetchone()[0] == 2001000
        returned.append(conn)
        original_return(conn)

    monkeypatch.setattr(corpus, "_return_read_conn", check_return)
    result = json.loads(session_search(query=query, role_filter=roles, detail=detail, db=corpus))
    assert result["success"] is False, result
    assert "search too broad" in result["error"].lower()
    assert "refine" in result["error"].lower()
    assert "results" not in result
    assert len(statements) == 1  # No fallback scan or partial success after interruption.
    assert returned


def test_nested_searches_share_budget_and_keep_tail_hits(corpus, monkeypatch, caplog):
    import hermes_state_search as search
    import hermes_state_search_runtime as runtime
    from hermes_state_common import FTS_TOOL_CONTENT_PREFIX_CHARS

    corpus.append_message("history", "tool", "x" * FTS_TOOL_CONTENT_PREFIX_CHARS + " tailneedle")
    clock = [runtime.time.monotonic()]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(search, "_search_slow_ms", lambda: 0)
    with caplog.at_level(logging.INFO, logger="hermes_state"):
        with runtime.search_operation(corpus):
            # The queryless outer scope must acquire a budget when a query starts.
            assert corpus.search_messages("tailneedle", role_filter=["tool"], fields=["id"])
            clock[0] += 31.0
            with pytest.raises(sqlite3.OperationalError, match="search too broad"):
                corpus.search_messages("common", role_filter=["tool"], fields=["id"])
    lines = [r.getMessage() for r in caplog.records if "slow session search:" in r.getMessage()]
    assert len(lines) == 1
    assert "path=like_scan" in lines[0]
    assert "rows=err" in lines[0]


@pytest.mark.parametrize("ignored_query", ["", "common"])
def test_browse_and_transcript_shapes_never_install_a_deadline(corpus, monkeypatch, ignored_query):
    original = corpus._checkout_read_conn

    class Reader:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def set_progress_handler(self, handler, steps):
            assert handler is None, "queryless recall must not install a search deadline"

    @contextmanager
    def no_deadline():
        def checkout(**kwargs):
            conn = original(**kwargs)
            return Reader(conn) if conn is not None else None

        original_return = corpus._return_read_conn
        monkeypatch.setattr(corpus, "_checkout_read_conn", checkout)
        monkeypatch.setattr(corpus, "_return_read_conn", lambda reader: original_return(reader.conn))
        yield

    with no_deadline():
        assert json.loads(session_search(db=corpus))["success"]
        assert json.loads(session_search(db=corpus, session_id="history", query=ignored_query))["success"]
        assert json.loads(session_search(
            db=corpus, session_id="history", around_message_id=1, query=ignored_query,
        ))["success"]
