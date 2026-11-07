"""Inline TUI workers must not replace threads owned by state diagnostics."""

import threading
from contextvars import ContextVar

from hermes_state import SessionDB
from tests.tui_gateway.test_goal_command import _InlineThread
from tui_gateway import server, session_lifecycle


def test_inline_tui_worker_keeps_process_threads_asynchronous(monkeypatch, tmp_path):
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    caller = threading.get_ident()
    inline_workers = []
    server.threading.Thread(target=lambda: inline_workers.append(threading.get_ident())).start()
    assert inline_workers == [caller]

    scope = ContextVar("thread-fixture-scope", default="unbound")
    token = scope.set("owning-profile")
    try:
        context_workers = []
        session_lifecycle._start_session_work(
            lambda: context_workers.append((threading.get_ident(), scope.get())),
            name="thread-fixture-context",
        )
        assert context_workers == [(caller, "owning-profile")]
    finally:
        scope.reset(token)

    workers = []
    thread = threading.Thread(target=lambda: workers.append(threading.get_ident()))
    thread.start()
    thread.join(timeout=5)
    assert workers and workers[0] != caller

    # The real state writer starts its own long-lived diagnostic helper on macOS.
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session(session_id="thread-isolation", source="tui", model="test")
        row = db.get_session("thread-isolation")
        assert row is not None and row["source"] == "tui"
