"""A dispatcher dying before PID publication must not duplicate an admitted card."""
import os
import time

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing, init_db
from hermes_cli import kanban_worker_client


def _claim(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    init_db(db)
    with connect_closing(db) as conn:
        tid = kb.create_task(conn, title="spawn before dispatcher death", assignee="default")
        task = kb.claim_task(conn, tid)
    for key, value in {"DB": db, "BOARD": "default", "TASK": tid, "RUN_ID": task.current_run_id,
                       "CLAIM_LOCK": task.claim_lock}.items():
        monkeypatch.setenv("HERMES_KANBAN_" + key, str(value))
    return db, task


def test_live_submitter_registers_before_owner_admission_and_keeps_expired_claim(tmp_path, monkeypatch):
    db, task = _claim(tmp_path, monkeypatch)
    admitted = []

    async def owner(params, board_db):
        assert board_db == str(db) and params["run_id"] == task.current_run_id
        with connect_closing(db) as conn:
            live = kb.get_task(conn, task.id)
            assert live.worker_pid == os.getpid()
            assert conn.execute("SELECT worker_started_at FROM tasks WHERE id=?", (task.id,)).fetchone()[0]
            conn.execute("UPDATE tasks SET claim_expires=? WHERE id=?", (int(time.time()) - 1, task.id))
            conn.commit()
            assert kb.release_stale_claims(conn) == 0
            assert kb.get_task(conn, task.id).current_run_id == task.current_run_id
        admitted.append(params)
        return 0

    monkeypatch.setattr(kanban_worker_client, "run", owner)
    assert kanban_worker_client._submit() == 0
    assert len(admitted) == 1


def test_reclaimed_submitter_never_contacts_owner_or_replaces_new_run(tmp_path, monkeypatch):
    db, task = _claim(tmp_path, monkeypatch)
    with connect_closing(db) as conn:
        assert kb.reclaim_task(conn, task.id)
        replacement = kb.claim_task(conn, task.id)

    async def owner(*_args):
        raise AssertionError("stale submitter contacted the owner")

    monkeypatch.setattr(kanban_worker_client, "run", owner)
    assert kanban_worker_client._submit() == 1
    with connect_closing(db) as conn:
        current = kb.get_task(conn, task.id)
        assert current.current_run_id == replacement.current_run_id and current.worker_pid is None
