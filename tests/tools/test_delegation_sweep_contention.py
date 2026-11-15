"""An idle retry sweep must not compete with the transcript writer."""

import sqlite3

from tools import async_delegation as ad
from tools import delegation_resume as dr


def test_idle_sweep_reads_while_another_connection_holds_the_writer(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setattr(ad, "_db_path", lambda: path)
    with ad._transaction() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    connect = ad._connect

    def short_connect():
        conn = connect()
        conn.execute("PRAGMA busy_timeout=20")
        return conn

    monkeypatch.setattr(ad, "_connect", short_connect)
    writer = sqlite3.connect(path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        # The lock is retained until the sweep returns, including its busy timeout.
        assert dr.sweep_retries() == []
        assert writer.in_transaction
    finally:
        writer.close()

    # A stop can commit after discovery but before writer admission. It must
    # win over the stale candidate without spending a notice or resume claim.
    now = 1_800_000_000.0
    with ad._transaction() as conn:
        conn.execute("""INSERT INTO async_delegations
            (delegation_id,origin_session,parent_session_id,state,dispatched_at,updated_at,
             task_json,result_json,delivery_state,retry_state,retry_due_at,retry_root_started_at)
            VALUES('stopped','agent:main:telegram:dm:1','parent','error',?,?,?,
                   '{}','delivered','scheduled',?,?)""",
            (now - 100, now - 100, '{"goal":"recover work"}', now - 1, now - 100))
    stopped = []

    def stop_before_begin(sql):
        if sql == "BEGIN IMMEDIATE" and not stopped:
            with sqlite3.connect(path) as other:
                other.execute("UPDATE async_delegations SET retry_state='cancelled' "
                              "WHERE delegation_id='stopped'")
            stopped.append(True)

    def racing_connect():
        conn = short_connect()
        conn.set_trace_callback(stop_before_begin)
        return conn

    monkeypatch.setattr(ad, "_connect", racing_connect)
    assert dr.sweep_retries(now=now) == []
    assert stopped == [True]
    with ad._transaction() as conn:
        assert conn.execute("SELECT retry_state,retry_claim,retry_notices,resume_attempts "
                            "FROM async_delegations WHERE delegation_id='stopped'").fetchone() == (
                                "cancelled", None, 0, 0)
