"""Epoch fencing must not depend on pytest's import-time isolation markers."""
import os

import pytest

from hermes_state import SessionDB
from hermes_state_runtime import RuntimeStoreError, begin_runtime_epoch


@pytest.mark.parametrize('existing', [False, True])
def test_unreserved_custom_home_cannot_advance_epoch(tmp_path, monkeypatch, existing):
    home = tmp_path / 'custom-home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    with SessionDB(home / 'state.db') as db:
        if existing:
            db._execute_write(lambda conn: conn.execute(
                "INSERT INTO runtime_epoch(singleton,epoch,instance_id) VALUES(1,35,'gateway')"))
        before = [tuple(row) for row in db._conn.execute('SELECT * FROM runtime_epoch')]
        refusal = None
        with monkeypatch.context() as unmarked:
            for name in tuple(os.environ):
                if name.startswith('PYTEST_') or name == 'HERMES_TEST_ISOLATION':
                    unmarked.delenv(name)
            try:
                begin_runtime_epoch(db, instance_id='ad-hoc-probe')
            except RuntimeStoreError as exc:
                refusal = exc.reason
        after = [tuple(row) for row in db._conn.execute('SELECT * FROM runtime_epoch')]
        assert (refusal, after) == ('not_owner', before)


@pytest.mark.asyncio
async def test_gateway_ownership_authorizes_epoch_in_worker_thread(tmp_path):
    import asyncio
    from gateway.runtime_ownership import process_ownership

    process_ownership.reserve([tmp_path])
    with SessionDB(tmp_path / 'state.db') as db:
        try:
            assert await asyncio.to_thread(begin_runtime_epoch, db, instance_id='gateway') == 1
            assert await asyncio.to_thread(begin_runtime_epoch, db, instance_id='gateway') == 2
        finally:
            process_ownership.release(tmp_path)
        with pytest.raises(RuntimeStoreError, match='not_owner'):
            begin_runtime_epoch(db, instance_id='released-owner')
        with db._read_ctx() as conn:
            assert tuple(conn.execute('SELECT epoch,instance_id FROM runtime_epoch').fetchone()) == (2, 'gateway')


def test_maintenance_ownership_is_exact_home_and_current_thread(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from gateway.runtime_ownership import exclusive_maintenance

    home, other = tmp_path / 'reserved', tmp_path / 'other'
    with SessionDB(home / 'state.db') as db, SessionDB(other / 'state.db') as other_db:
        with exclusive_maintenance([home]):
            assert begin_runtime_epoch(db, instance_id='maintenance') == 1
            with pytest.raises(RuntimeStoreError, match='not_owner'):
                begin_runtime_epoch(other_db, instance_id='wrong-home')
            with ThreadPoolExecutor(max_workers=1) as pool:
                with pytest.raises(RuntimeStoreError, match='not_owner'):
                    pool.submit(begin_runtime_epoch, db, instance_id='wrong-thread').result()
        with pytest.raises(RuntimeStoreError, match='not_owner'):
            begin_runtime_epoch(db, instance_id='expired-maintenance')
        with db._read_ctx() as conn:
            assert tuple(conn.execute('SELECT epoch,instance_id FROM runtime_epoch').fetchone()) == (1, 'maintenance')
        with other_db._read_ctx() as conn:
            assert conn.execute('SELECT COUNT(*) FROM runtime_epoch').fetchone()[0] == 0


def test_dashboard_offline_maintenance_initializes_epoch(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli.web_server_sessions import _mutate_offline_store

    import hermes_state

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    # The autouse isolation fixture also pins DEFAULT_DB_PATH; keep the offline
    # route and the session fixture on the same disposable store.
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', tmp_path / 'state.db')
    with SessionDB() as db:
        db.create_session('offline', 'cli')
        assert db.get_session('offline') is not None
        with db._read_ctx() as conn:
            assert conn.execute('SELECT COUNT(*) FROM runtime_epoch').fetchone()[0] == 0
    _mutate_offline_store(None, SimpleNamespace(subject='dashboard'), {
        'session_id': 'offline', 'request_id': 'rename', 'expected_revision': 0,
        'operation': 'rename', 'payload': {'title': 'Offline Edit'},
    })
    with SessionDB(tmp_path / 'state.db', read_only=True) as db:
        with db._read_ctx() as conn:
            assert tuple(conn.execute('SELECT epoch,instance_id FROM runtime_epoch').fetchone()) == (1, 'dashboard-offline')
            assert conn.execute("SELECT title FROM sessions WHERE id='offline'").fetchone()[0] == 'Offline Edit'


def test_backup_restore_stages_epoch_floor_without_advancing_it(tmp_path):
    import sqlite3
    from hermes_cli.backup_restore import _restore_epoch_source

    source, destination = tmp_path / 'snapshot.db', tmp_path / 'state.db'
    for path, epoch in ((source, 35), (destination, 36)):
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE runtime_epoch(singleton INTEGER PRIMARY KEY, epoch INTEGER, instance_id TEXT)')
            conn.execute('INSERT INTO runtime_epoch VALUES(1,?,?)', (epoch, 'original'))
    before = {path: path.read_bytes() for path in (source, destination)}
    with _restore_epoch_source(source, destination) as prepared:
        with sqlite3.connect(prepared) as conn:
            assert conn.execute('SELECT epoch,instance_id FROM runtime_epoch').fetchone() == (36, 'offline-restore')
        assert prepared != source
    assert {path: path.read_bytes() for path in (source, destination)} == before
