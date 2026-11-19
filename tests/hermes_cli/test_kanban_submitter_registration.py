"""A dispatcher dying before PID publication must not duplicate an admitted card."""
from contextlib import closing
import os
import sqlite3
import time

import pytest

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


@pytest.mark.parametrize('refusal', ['missing_board', 'unreadable_board'])
def test_registration_refusal_never_submits_work(tmp_path, monkeypatch, refusal, capsys):
    """Registration failures fail closed before owner startup or admission."""
    from hermes_cli import kanban_db as kb, kanban_db_connect

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    path = tmp_path / 'board.db'
    tid, run_id = 't_stale', 1
    if refusal != 'missing_board':
        with closing(kanban_db_connect.connect(path)) as conn:
            tid = kb.create_task(conn, title='stale submitter', assignee='default')
            task = kb.claim_task(conn, tid)
            run_id = task.current_run_id
            kb.block_task(conn, tid, reason='replaced', expected_run_id=run_id)
    for name, value in {'BOARD': 'default', 'TASK': tid, 'RUN_ID': str(run_id),
                        'CLAIM_LOCK': 'old', 'DB': str(path)}.items():
        monkeypatch.setenv('HERMES_KANBAN_' + name, value)
    if refusal == 'unreadable_board':
        def unreadable(*args, **kwargs):
            raise sqlite3.OperationalError('board unavailable')
        monkeypatch.setattr(kanban_db_connect, 'connect_closing', unreadable)

    from hermes_cli import kanban_worker_client

    def forbidden(*args, **kwargs):
        pytest.fail('registration refusal reached owner startup/admission')

    monkeypatch.setattr(kanban_worker_client, 'run', forbidden)
    assert kanban_worker_client.main() == 1
    stderr = capsys.readouterr().err
    assert 'Kanban owner refused:' in stderr
    assert '[kanban-worker-exit] rc=1' in stderr
    assert 'Traceback' not in stderr
    if refusal == 'missing_board':
        assert not path.exists()


def test_owner_sqlite_error_keeps_its_traceback(tmp_path, monkeypatch):
    db, _task = _claim(tmp_path, monkeypatch)
    from hermes_cli import kanban_worker_client

    async def owner(*_args, **_kwargs):
        raise sqlite3.OperationalError('owner admission failed')

    monkeypatch.setattr(kanban_worker_client, 'run', owner)
    with pytest.raises(sqlite3.OperationalError, match='owner admission failed'):
        kanban_worker_client._submit()
