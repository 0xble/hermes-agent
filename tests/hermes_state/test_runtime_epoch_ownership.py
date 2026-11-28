"""Runtime epoch ownership must reject unreserved writers and preserve live-owner fencing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gateway.runtime_ownership import OwnershipConflict
from hermes_state import SessionDB
from tests.fakes.runtime_owner import begin_runtime_epoch as _seed_epoch
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


_REPO_ROOT = Path(__file__).resolve().parents[2]

_HOLDER = """
import sys
from pathlib import Path
from gateway.runtime_ownership import ProfileOwnership
ownership = ProfileOwnership()
ownership.reserve([Path(sys.argv[1])])
print('ready', flush=True)
sys.stdin.read()  # held until the fixture closes stdin (or the test process dies)
"""


@pytest.fixture()
def hold_profile():
    """Start a foreign process that really reserves a profile's ``gateway.lock`` (the gateway)."""
    procs = []

    def start(home):
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(_REPO_ROOT), os.environ.get("PYTHONPATH")]))}
        proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(home)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True, env=env, cwd=_REPO_ROOT)
        procs.append(proc)
        assert proc.stdout.readline().strip() == "ready"
        return proc.pid

    yield start
    for proc in procs:
        proc.stdin.close()
        proc.wait(timeout=10)


def _epoch_of(db) -> int | None:
    row = db._conn.execute("SELECT epoch FROM runtime_epoch WHERE singleton=1").fetchone()
    return row[0] if row else None


def test_a_stranger_cannot_bump_the_epoch_of_a_live_owners_store(tmp_path, hold_profile):
    """The reported failure: a process that does not own the profile must not fence the owner out."""
    import hermes_state_runtime as rt

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        epoch = _seed_epoch(db, instance_id="owner")
        hold_profile(tmp_path)

        with pytest.raises(OwnershipConflict):
            rt.begin_runtime_epoch(db, instance_id="stranger")

        # The owner's fence must still stand: the bump did not move.
        assert _epoch_of(db) == epoch
    finally:
        db.close()


def test_an_unpublished_holder_still_fences_the_bump(tmp_path, hold_profile):
    """The flock decides, not the record: a holder whose record is torn/unreadable still owns it."""
    import hermes_state_runtime as rt

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        epoch = _seed_epoch(db, instance_id="owner")
        hold_profile(tmp_path)
        (tmp_path / "gateway.lock").write_text("", encoding="utf-8")  # same inode, record gone

        with pytest.raises(OwnershipConflict):
            rt.begin_runtime_epoch(db, instance_id="stranger")
        assert _epoch_of(db) == epoch
    finally:
        db.close()


def test_a_record_naming_this_pid_in_another_pid_namespace_is_not_ours(tmp_path, hold_profile):
    """Same pid number, other PID namespace (a containerised holder): the flock decides, not the pid."""
    import hermes_state_runtime as rt
    from gateway.status import _build_pid_record

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        epoch = _seed_epoch(db, instance_id="owner")
        hold_profile(tmp_path)
        record = {**_build_pid_record(), "pidns": "another-namespace"}
        (tmp_path / "gateway.lock").write_text(json.dumps(record), encoding="utf-8")

        with pytest.raises(OwnershipConflict):
            rt.begin_runtime_epoch(db, instance_id="stranger")
        assert _epoch_of(db) == epoch
    finally:
        db.close()


def test_our_released_reservations_record_does_not_grant_the_bump(tmp_path, hold_profile):
    """A record naming this process outlives our release: a foreign holder that took the flock
    since but has not rewritten it yet must still fence the bump."""
    import hermes_state_runtime as rt
    from gateway.runtime_ownership import ProfileOwnership

    db = SessionDB(db_path=tmp_path / "state.db")
    ownership = ProfileOwnership()
    try:
        epoch = _seed_epoch(db, instance_id="owner")
        ownership.reserve([tmp_path])
        ours = (tmp_path / "gateway.lock").read_text(encoding="utf-8")
        ownership.release(tmp_path)
        hold_profile(tmp_path)
        (tmp_path / "gateway.lock").write_text(ours, encoding="utf-8")  # the holder has not published

        with pytest.raises(OwnershipConflict):
            rt.begin_runtime_epoch(db, instance_id="stranger")
        assert _epoch_of(db) == epoch
    finally:
        ownership.close()
        db.close()


@pytest.mark.platforms("any")
def test_the_owner_itself_may_bump_the_epoch(tmp_path):
    """The holder of the profile lock is the one taking the store over - it must be allowed."""
    import hermes_state_runtime as rt
    from gateway.runtime_ownership import exclusive_maintenance

    with SessionDB(db_path=tmp_path / "state.db") as db, exclusive_maintenance([tmp_path]):
        first = rt.begin_runtime_epoch(db, instance_id="owner")
        second = rt.begin_runtime_epoch(db, instance_id="owner-again")
        assert second > first


def test_the_verdict_is_per_profile(tmp_path, hold_profile):
    """A -> B: the owned profile refuses the stranger, and the other profile is untouched.

    The epoch row is a singleton of ITS store, so the refusal is about the profile that was
    asked - a second profile's own store is still free to be taken over."""
    import hermes_state_runtime as rt

    owned = tmp_path / "owned"
    free = tmp_path / "free"
    owned.mkdir()
    free.mkdir()
    hold_profile(owned)

    db_owned = SessionDB(db_path=owned / "state.db")
    db_free = SessionDB(db_path=free / "state.db")
    try:
        with pytest.raises(OwnershipConflict):
            rt.begin_runtime_epoch(db_owned, instance_id="stranger")
        assert _epoch_of(db_owned) is None, "the refused bump must not have created the row"

        # The other profile's store is a different store: free to be taken over.
        assert _seed_epoch(db_free, instance_id="owner") == 1
    finally:
        db_owned.close()
        db_free.close()


def test_an_unowned_store_can_be_taken_over(tmp_path):
    """No live owner: the bump IS the takeover (the gateway's own boot, and every test store)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        epoch = _seed_epoch(db, instance_id="first-owner")
        assert epoch == 1
        replacement = _seed_epoch(db, instance_id="replacement")
        assert replacement > epoch
    finally:
        db.close()


def test_a_dead_owners_record_does_not_fence_the_bump(tmp_path):
    """A stale lock record (the gateway died, the flock went with it): the store can be taken over."""
    from gateway.status import _build_pid_record

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        first = _seed_epoch(db, instance_id="owner")
        record = {**_build_pid_record(), "pid": 999_999_999, "start_time": None}
        (tmp_path / "gateway.lock").write_text(json.dumps(record), encoding="utf-8")
        replacement = _seed_epoch(db, instance_id="replacement")
        assert replacement > first
    finally:
        db.close()