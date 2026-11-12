"""Read-only ledger access must survive work-connection closes and idle ticker waits."""

from contextlib import closing
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

from cron import executions
from cron.scheduler_provider import InProcessCronScheduler, _profile_cron_scope


def _read_only(path):
    # Some SQLite builds can create WAL/SHM from a read-only main DB handle;
    # assert the actual lifetime contract too, so those builds detect regressions.
    assert Path(str(path) + "-wal").exists()
    assert Path(str(path) + "-shm").exists()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)) as conn:
        return conn.execute("SELECT count(*) FROM executions").fetchone()[0]


@pytest.mark.parametrize("multiplex", [False, True])
def test_ticker_keeps_ro_readers_available_between_work_and_ticks(tmp_path, monkeypatch, multiplex):
    from cron import scheduler
    import cron.scheduler_provider as provider
    import hermes_constants

    homes = [tmp_path / "primary"]
    if multiplex:
        homes.append(tmp_path / "secondary")
    for home in homes:
        home.mkdir()
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", None)
    monkeypatch.setattr(hermes_constants, "get_process_hermes_home", lambda: homes[0])
    monkeypatch.setenv("HERMES_HOME", str(homes[0]))
    stop = Mock()
    stop.is_set.return_value = False
    ticks = []
    waits = []

    def tick(**kwargs):
        home = hermes_constants.get_hermes_home().resolve()
        path = home / "cron" / "executions.db"
        for _ in range(3):
            with executions._transaction() as conn:
                conn.execute("SELECT count(*) FROM executions").fetchone()
            assert _read_only(path) == 0
        ticks.append(home)

    def wait(*args, **kwargs):
        for home in homes:
            assert _read_only(home / "cron" / "executions.db") == 0
        waits.append(True)
        if len(waits) == 3:
            stop.is_set.return_value = True

    monkeypatch.setattr(scheduler, "tick", tick)
    monkeypatch.setattr(provider, "wait_for_next_tick", wait)
    with _profile_cron_scope(homes[0]):
        InProcessCronScheduler().start(
            stop, profile_homes=homes if multiplex else None,
        )
    # Tick exceptions are contained by the scheduler; require all callbacks to
    # finish, so a failed assertion cannot be swallowed into a false green.
    assert len(ticks) == len(homes) * 3
    assert len(waits) == 3
    for home in homes:
        assert not Path(str(home / "cron" / "executions.db") + "-wal").exists()
        assert not Path(str(home / "cron" / "executions.db") + "-shm").exists()


def test_owner_reuses_handle_allows_writes_and_closes_departed_homes(tmp_path, monkeypatch):
    from cron.scheduler_store import ExecutionStoreOwners

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", None)
    homes = [tmp_path / "one", tmp_path / "two"]
    for home in homes:
        home.mkdir()
    with ExecutionStoreOwners() as owners:
        for home in homes:
            with _profile_cron_scope(home):
                owners.retain(home)
                owners.retain(home)
                record = executions.create_execution("job", source="test")
                assert _read_only(home / "cron" / "executions.db") == 1
                executions.finish_execution(record["id"], success=True)
        owners.discard_except([homes[1]])
        assert not Path(str(homes[0] / "cron" / "executions.db") + "-wal").exists()
        assert _read_only(homes[1] / "cron" / "executions.db") == 1
    assert not Path(str(homes[1] / "cron" / "executions.db") + "-wal").exists()


def test_owner_initialization_failure_closes_and_can_retry(tmp_path, monkeypatch):
    from cron.scheduler_store import ExecutionStoreOwners

    conn = Mock()
    conn.execute.side_effect = sqlite3.OperationalError("database is locked")
    connect = Mock(return_value=conn)
    monkeypatch.setattr(executions, "_connect", connect)
    with ExecutionStoreOwners() as owners:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            owners.retain(tmp_path)
        conn.close.assert_called_once()
        conn.execute.side_effect = None
        owners.retain(tmp_path)
        assert connect.call_count == 2
    assert conn.close.call_count == 2


@pytest.mark.parametrize("multiplex", [False, True])
def test_owner_failure_keeps_tick_housekeeping_and_backoff(tmp_path, monkeypatch, multiplex):
    import errno
    from cron import scheduler
    import cron.scheduler_provider as provider
    import cron.scheduler_store as store
    import hermes_constants

    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setattr(hermes_constants, "get_process_hermes_home", lambda: home)
    monkeypatch.setattr(InProcessCronScheduler, "recover_interrupted", lambda self: 0)
    retain = Mock(side_effect=OSError(errno.EMFILE, "Too many open files"))
    monkeypatch.setattr(store.ExecutionStoreOwners, "retain", retain)
    tick = Mock()
    monkeypatch.setattr(scheduler, "tick", tick)
    stop = Mock()
    stop.is_set.return_value = False
    waits = []

    def wait(*args, **kwargs):
        waits.append(kwargs["failures"])
        stop.is_set.return_value = True

    monkeypatch.setattr(provider, "wait_for_next_tick", wait)
    with _profile_cron_scope(home):
        InProcessCronScheduler().start(stop, profile_homes=[home] if multiplex else None)
    tick.assert_called_once()
    assert waits == [1]
    assert retain.call_count == 2  # startup failure is retried before the work cycle


def test_multiplex_owners_follow_successful_enumeration_and_gate(tmp_path, monkeypatch):
    from cron import scheduler
    import cron.scheduler_provider as provider

    first, added, rejected = (tmp_path / name for name in ("first", "added", "rejected"))
    for home in (first, added, rejected):
        home.mkdir()
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", None)
    monkeypatch.setattr(InProcessCronScheduler, "recover_interrupted", lambda self: 0)
    cycles = {"waits": 0, "enumerations": 0}
    ticked = []
    stop = Mock()
    stop.is_set.return_value = False

    def enumerate_homes():
        cycles["enumerations"] += 1
        if cycles["waits"] == 1:
            raise OSError("transient enumeration failure")
        if cycles["waits"] >= 3:
            return []
        return [first, rejected] + ([added] if cycles["waits"] >= 2 else [])

    def tick(**kwargs):
        import hermes_constants
        home = hermes_constants.get_hermes_home()
        # First tick of a newly served home must already have an owner.
        with executions._transaction() as conn:
            conn.execute("SELECT 1").fetchone()
        assert _read_only(home / "cron" / "executions.db") == 0
        ticked.append(home)

    def wait(*args, **kwargs):
        n = cycles["waits"]
        if n < 3:
            assert _read_only(first / "cron" / "executions.db") == 0
        else:
            assert not Path(str(first / "cron" / "executions.db") + "-wal").exists()
            assert not Path(str(added / "cron" / "executions.db") + "-wal").exists()
        assert not (rejected / "cron" / "executions.db").exists()
        cycles["waits"] += 1
        if cycles["waits"] == 4:
            stop.is_set.return_value = True

    monkeypatch.setattr(scheduler, "tick", tick)
    monkeypatch.setattr(provider, "wait_for_next_tick", wait)
    InProcessCronScheduler().start(
        stop, profile_homes=enumerate_homes,
        profile_gate=lambda name, home: home != rejected,
    )
    assert ticked == [first, first, added]
    assert cycles["waits"] == 4
