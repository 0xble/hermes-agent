"""An unregistered submitter must never reach owner admission."""
from contextlib import closing
import sqlite3

import pytest


@pytest.mark.parametrize('refusal', ['missing_board', 'unreadable_board', 'reclaimed_run'])
def test_registration_refusal_never_submits_work(tmp_path, monkeypatch, refusal, capsys):
    from hermes_cli import kanban_db as kb, kanban_db_connect, kanban_worker_client

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
