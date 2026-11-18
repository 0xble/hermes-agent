"""A WAL snapshot remains fixed while an independent writer keeps committing."""

import sqlite3


def test_wal_copy_finishes_from_one_snapshot_while_writers_continue(
    tmp_path, monkeypatch
):
    from hermes_cli import backup as backup_mod

    src = tmp_path / "hot.db"
    dst = tmp_path / "copy.db"
    initial = b"snapshot" * 400
    with sqlite3.connect(src) as setup:
        assert setup.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        setup.execute("CREATE TABLE payload (id INTEGER PRIMARY KEY, value BLOB)")
        setup.executemany(
            "INSERT INTO payload(value) VALUES (?)",
            [(initial,) for _ in range(3000)],
        )

    import threading
    from contextlib import closing

    real_connect = backup_mod.sqlite3.connect
    remaining_values = []
    writes = 0
    start_writer, committed, stop_writer = threading.Event(), threading.Event(), threading.Event()
    writer_errors = []

    def write_continuously():
        nonlocal writes
        try:
            with closing(real_connect(src, timeout=1.0)) as writer:
                start_writer.wait()
                while not stop_writer.is_set():
                    writer.execute(
                        "UPDATE payload SET value = ? WHERE id = 1",
                        (f"write-{writes}".encode() * 400,),
                    )
                    writer.commit()
                    writes += 1
                    committed.set()
        except Exception as exc:
            writer_errors.append(exc)
            committed.set()

    class SourceConnection:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, *args, **kwargs):
            return self._connection.execute(*args, **kwargs)

        def backup(self, target, *, pages, progress, sleep):
            # The source read snapshot is already pinned. Keep a real writer active
            # across copying, including upstream's single-step off-thread WAL path.
            start_writer.set()
            assert committed.wait(5), "writer never committed while the snapshot was pinned"
            if writer_errors:
                raise writer_errors[0]

            def check_progress(status, remaining, total):
                progress(status, remaining, total)
                remaining_values.append(remaining)
                if len(remaining_values) >= 32 and remaining:
                    raise TimeoutError("backup kept restarting under WAL writes")

            try:
                return self._connection.backup(
                    target,
                    pages=pages,
                    progress=check_progress,
                    sleep=sleep,
                )
            finally:
                stop_writer.set()

        def close(self):
            self._connection.close()

    def connect(path, *args, **kwargs):
        connection = real_connect(path, *args, **kwargs)
        if isinstance(path, str) and path.startswith("file:"):
            return SourceConnection(connection)
        return connection

    monkeypatch.setattr(backup_mod.sqlite3, "connect", connect)
    worker = threading.Thread(target=write_continuously, daemon=True)
    worker.start()
    try:
        assert backup_mod._safe_copy_db(src, dst) is True
    finally:
        stop_writer.set()
        start_writer.set()
        worker.join(5)
    assert not worker.is_alive(), "snapshot writer did not stop"
    if writer_errors:
        raise writer_errors[0]

    assert writes > 0, "the writer must commit while the snapshot is being copied"
    assert remaining_values[-1] == 0
    with sqlite3.connect(dst) as copied:
        assert copied.execute("SELECT value FROM payload WHERE id = 1").fetchone() == (initial,)
    with sqlite3.connect(src) as live:
        assert live.execute("SELECT value FROM payload WHERE id = 1").fetchone() != (initial,)
