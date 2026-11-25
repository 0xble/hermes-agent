"""Recall stays reader-only even while token accounting and admission are busy."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import Context
import json
import sqlite3
import threading
import time

import pytest

from hermes_state import SessionDB
from tools.session_search_tool import session_search


@pytest.fixture
def db(tmp_path):
    store = SessionDB(db_path=tmp_path / "state.db")
    store.create_session("root", source="cli", model="test")
    store.append_message("root", "user", "old needle")
    parent = "root"
    for sid in ("middle", "tip"):
        store.end_session(parent, "compression")
        store.create_session(sid, source="cli", model="test", parent_session_id=parent)
        parent = sid
    store.set_session_title("root", "remembered title")
    store.append_message("tip", "user", "needle remembered")
    yield store
    store.close()


def test_all_tool_shapes_bypass_busy_token_writer(db):
    from tools.session_search_tool import _resolve_to_parent
    from hermes_state_search_runtime import search_operation

    anchor = db.get_messages("tip")[0]["id"]
    calls = [
        ("discover", dict(query="needle", detail="full", current_session_id="tip")),
        ("title", dict(query="remembered title", detail="full")),
        ("read", dict(session_id="tip")),
        ("scroll", dict(session_id="root", around_message_id=anchor)),
        ("browse", dict(current_session_id="tip")),
        ("excluded lineage", dict(query="needle", exclude_session_ids=["tip"])),
    ]

    def recall():
        timings = {}
        for shape, args in calls:
            started = time.monotonic()
            payload = json.loads(session_search(db=db, **args))
            timings[shape] = time.monotonic() - started
            assert payload["success"], payload
            if shape in {"discover", "title"}:
                assert payload["count"] == 1
                assert payload["results"][0]["messages"]
            if shape == "scroll":
                assert payload["session_id"] == "tip"
                assert payload["messages"]
        started = time.monotonic()
        with search_operation(db):
            assert _resolve_to_parent(db, "tip") == ("root", True)
        timings["3-ancestor lineage"] = time.monotonic() - started
        print("busy token writer timings (seconds):", json.dumps(timings, sort_keys=True))
        assert max(timings.values()) < 1
        return timings

    with ThreadPoolExecutor(max_workers=1) as executor:
        db._lock.acquire()
        with db._token_queue_cond:
            db._token_writer_busy = True
            db._token_queue.append(("tip", {"input_tokens": 1}))
        try:
            timings = executor.submit(recall).result(timeout=2)
            assert len(timings) == len(calls) + 1
        finally:
            with db._token_queue_cond:
                db._token_queue.clear()
                db._token_writer_busy = False
                db._token_queue_cond.notify_all()
            db._lock.release()


@pytest.mark.parametrize("mode", ["browse", "read", "scroll", "discover"])
def test_tool_stage_log_accounts_for_every_read(db, monkeypatch, caplog, mode):
    import logging
    import re
    from functools import partial
    from types import SimpleNamespace

    import hermes_state_search
    import hermes_state_search_runtime as runtime

    anchor = db.get_messages("tip")[0]["id"]
    calls = {
        "browse": {},
        "read": {"session_id": "tip"},
        "scroll": {"session_id": "root", "around_message_id": anchor},
        "discover": {"query": "needle", "detail": "full"},
    }
    clock, reads = [time.monotonic()], []

    def trace(sql):
        if sql.lstrip().upper().startswith(("SELECT", "WITH")):
            reads.append(sql)
            clock[0] += .025

    checkout = db._checkout_read_conn

    def traced_checkout(*args, **kwargs):
        conn = checkout(*args, **kwargs)
        if conn is not None:
            conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(db, "_checkout_read_conn", traced_checkout)
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(runtime, "SearchOperation", partial(runtime.SearchOperation, started=clock[0]))
    monkeypatch.setattr(hermes_state_search, "_search_slow_ms", lambda: 0)
    with caplog.at_level(logging.INFO, logger="hermes_state"):
        payload = json.loads(session_search(db=db, **calls[mode]))
    assert payload["success"], payload
    lines = [r.getMessage() for r in caplog.records if "slow session search:" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    rows = (payload["count"] if mode == "browse" else
            payload["message_count"] if mode == "read" else
            len(payload["messages"]) if mode == "scroll" else 2)
    assert f"rows={rows} " in line
    assert f"path={'fts5' if mode == 'discover' else mode} " in line
    values = {key: float(value) for key, value in re.findall(r"(\w+_ms)=(\d+\.\d+)", line)}
    assert reads
    assert values["sql_ms"] == pytest.approx(len(reads) * 25)
    assert values["hydration_ms"] == 0
    assert values["total_ms"] == pytest.approx(len(reads) * 25)


def test_get_session_keeps_exact_totals_outside_search(db, monkeypatch):
    from hermes_state_search_runtime import search_operation

    flushes = []
    monkeypatch.setattr(db, "flush_token_counts", lambda: flushes.append(True))
    with search_operation(db):
        assert db.get_session("tip")["id"] == "tip"
    assert flushes == []
    assert db.get_session("tip")["id"] == "tip"
    assert flushes == [True]


def test_priority_rechecks_after_slow_checkout(db, monkeypatch):
    from hermes_state import _READ_POOL_MAX
    from hermes_state_search_runtime import _ReaderQueue
    from tests.waiting import wait_until

    held = [db._checkout_read_conn() for _ in range(_READ_POOL_MAX - 1)]
    queue = _ReaderQueue()
    checked_out, release = threading.Event(), threading.Event()
    original = db._checkout_read_conn
    order = []

    def checkout(**kwargs):
        conn = original(**kwargs)
        if threading.current_thread().name == "background" and not checked_out.is_set():
            checked_out.set()
            assert release.wait(5)
        return conn

    def recall(priority, name):
        threading.current_thread().name = name
        conn = queue.checkout(db, priority)
        try:
            order.append(name)
        finally:
            db._return_read_conn(conn)
            with queue.condition:
                queue.condition.notify_all()

    monkeypatch.setattr(db, "_checkout_read_conn", checkout)
    with ThreadPoolExecutor(max_workers=2) as executor:
        background = executor.submit(recall, 1, "background")
        try:
            assert checked_out.wait(5)
            human = executor.submit(recall, 0, "human")
            wait_until(lambda: len(queue.waiters) == 2)
            release.set()
            human.result(timeout=5)
            background.result(timeout=5)
            assert order == ["human", "background"]
            assert queue.waiters == []
        finally:
            release.set()
            for conn in held:
                db._return_read_conn(conn)


@pytest.mark.parametrize("engaged", [False, True])
def test_priority_uses_only_bound_source(monkeypatch, engaged):
    import gateway.session_context as context
    from hermes_state_search_runtime import _priority

    for name, value in {
        "HERMES_SESSION_SOURCE": "cron",
        "HERMES_SESSION_PLATFORM": "subagent",
        "HERMES_TURN_ORIGIN": "cron",
        "HERMES_CRON_SESSION": "1",
        "HERMES_DELEGATED_CHILD_CONTEXT": "1",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(context, "_session_context_engaged", engaged)
    assert Context().run(_priority) == 0

    def bound(source):
        tokens = context.set_session_vars(source=source)
        try:
            return _priority()
        finally:
            context.clear_session_vars(tokens)

    assert Context().run(bound, "cron") == 1
    assert Context().run(bound, "telegram") == 0


@pytest.mark.parametrize("identity", [
    {"turn_origin": "cron"},
    {"cron_session": "1"},
    {"platform": "cron"},
    {"platform": "subagent"},
])
def test_priority_recognizes_bound_background_identity(identity):
    import gateway.session_context as context
    from hermes_state_search_runtime import _priority

    def bound():
        tokens = context.set_session_vars(source="telegram", **identity)
        try:
            assert _priority() == 1
        finally:
            context.clear_session_vars(tokens)

    Context().run(bound)


def test_priority_recognizes_production_cron_environment(monkeypatch):
    from cron import scheduler
    from hermes_state_search_runtime import _priority

    monkeypatch.setattr(scheduler, "_resolve_job_workdir", lambda *_args: None)

    def run():
        environment = scheduler._CronRunScope({}, "search-priority", None)
        environment.enter()
        try:
            assert _priority() == 1
        finally:
            environment.exit()

    Context().run(run)


def test_priority_recognizes_delegated_child_with_parent_source():
    import gateway.session_context as context
    from agent.delegation_context import delegated_child_context
    from hermes_state_search_runtime import _priority

    def run():
        tokens = context.set_session_vars(source="telegram", platform="telegram", turn_origin="user")
        try:
            assert _priority() == 0
            with delegated_child_context():
                assert _priority() == 1
            assert _priority() == 0
        finally:
            context.clear_session_vars(tokens)

    Context().run(run)


@pytest.mark.parametrize("message", ["database is locked", "interrupted", "disk I/O error"])
def test_gap_failure_never_returns_partial_success(db, monkeypatch, message):
    monkeypatch.setattr(db, "fts_rebuild_status", lambda: {"percent": 50, "total": 2, "indexed": 1})

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr(db, "_search_unindexed_gap", fail)
    with pytest.raises(sqlite3.OperationalError, match=message):
        db.search_messages("needle", limit=10)
    payload = json.loads(session_search(db=db, query="needle", detail="full"))
    assert payload["success"] is False
    assert message in payload["error"]


def test_gap_syntax_error_can_keep_valid_fts_matches(db, monkeypatch):
    monkeypatch.setattr(db, "fts_rebuild_status", lambda: {"percent": 50, "total": 2, "indexed": 1})

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('fts5: syntax error near "*"')

    monkeypatch.setattr(db, "_search_unindexed_gap", fail)
    assert db.search_messages("needle", limit=10)


def test_tool_browse_has_no_admitted_sql_deadline(db, monkeypatch):
    from contextlib import contextmanager

    original = db._read_ctx

    class Reader:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, *args, **kwargs):
            return self.conn.execute(*args, **kwargs)

        def set_progress_handler(self, handler, steps):
            assert handler is None, "admitted recall must not install a SQL timeout"

    @contextmanager
    def read():
        with original() as conn:
            yield Reader(conn)

    monkeypatch.setattr(db, "_read_ctx", read)
    payload = json.loads(session_search(db=db))
    assert payload["success"], payload
    assert payload["count"] == 1


def test_open_failure_uses_database_unavailable_message(monkeypatch):
    from hermes_state import format_session_db_unavailable
    import hermes_state_registry

    def fail():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(hermes_state_registry, "acquire", fail)
    payload = json.loads(session_search(query="needle"))
    assert payload["success"] is False
    assert payload["error"] == format_session_db_unavailable()


def test_read_sql_error_is_not_rewritten_as_a_missing_session(db, monkeypatch):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("interrupted")

    monkeypatch.setattr(db, "get_messages", fail)
    payload = json.loads(session_search(db=db, session_id="tip"))
    assert payload["success"] is False
    assert "interrupted" in payload["error"]
    assert "not found" not in payload["error"]
