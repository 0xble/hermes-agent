"""Tests for hermes backup and import commands."""

import json
import os
import sqlite3
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest

from tests.hermes_cli.backup_support import (
    _advance_backup_clock,
    _fail_zip_write_after,
    _make_hermes_tree,
    _no_real_gateway_service as _no_real_gateway_service,
    _restore_backup_datetime as _restore_backup_datetime,
    _symlink_file_or_skip,
)


class TestSafeCopyDb:
    def test_copies_valid_database(self, tmp_path):
        from hermes_cli.backup import _safe_copy_db
        src = tmp_path / "test.db"
        dst = tmp_path / "copy.db"

        conn = sqlite3.connect(str(src))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()
        conn.close()

        result = _safe_copy_db(src, dst)
        assert result is True

        conn = sqlite3.connect(str(dst))
        rows = conn.execute("SELECT x FROM t").fetchall()
        conn.close()
        assert rows == [(42,)]

    def test_skips_snapshot_when_destination_volume_is_too_full(self, tmp_path, monkeypatch):
        from hermes_cli import backup_sqlite

        src = tmp_path / "large.db"
        dst = tmp_path / "copy.db"
        src.write_bytes(b"database image")
        monkeypatch.setattr(
            backup_sqlite.shutil,
            "disk_usage",
            lambda _path: Namespace(free=0),
        )

        assert backup_sqlite._safe_copy_db(src, dst) is False
        assert not dst.exists()

    def test_wal_copy_finishes_from_one_snapshot_while_writers_continue(
        self, tmp_path, monkeypatch
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

        real_connect = backup_mod.sqlite3.connect
        writer = real_connect(src, timeout=1.0, check_same_thread=False)
        remaining_values = []
        writes = 0

        class SourceConnection:
            def __init__(self, connection):
                self._connection = connection

            def execute(self, *args, **kwargs):
                return self._connection.execute(*args, **kwargs)

            def backup(self, target, *, pages, progress, sleep):
                def write_between_steps(status, remaining, total):
                    nonlocal writes
                    progress(status, remaining, total)
                    remaining_values.append(remaining)
                    # A one-step WAL copy reports only SQLITE_DONE. Commit while
                    # backup() still owns its pinned snapshot on the copy thread.
                    writer.execute(
                        "UPDATE payload SET value = ? WHERE id = 1",
                        (f"write-{writes}".encode() * 400,),
                    )
                    writer.commit()
                    writes += 1
                    if len(remaining_values) >= 32 and remaining:
                        raise TimeoutError("backup kept restarting under WAL writes")

                return self._connection.backup(
                    target,
                    pages=pages,
                    progress=write_between_steps,
                    sleep=sleep,
                )

            def close(self):
                self._connection.close()

        def connect(path, *args, **kwargs):
            connection = real_connect(path, *args, **kwargs)
            if isinstance(path, str) and path.startswith("file:"):
                return SourceConnection(connection)
            return connection

        monkeypatch.setattr(backup_mod.sqlite3, "connect", connect)
        try:
            assert backup_mod._safe_copy_db(src, dst) is True
        finally:
            writer.close()

        assert writes > 0, "the writer must commit while the snapshot is being copied"
        assert remaining_values[-1] == 0
        with sqlite3.connect(dst) as copied:
            assert copied.execute("SELECT value FROM payload WHERE id = 1").fetchone() == (initial,)
        with sqlite3.connect(src) as live:
            assert live.execute("SELECT value FROM payload WHERE id = 1").fetchone() != (initial,)

    def test_transient_rollback_writer_does_not_abort_snapshot_setup(self, tmp_path):
        import subprocess
        import sys

        from hermes_cli.backup import _safe_copy_db

        src = tmp_path / "rollback.db"
        dst = tmp_path / "copy.db"
        with sqlite3.connect(src) as setup:
            setup.execute("CREATE TABLE payload (value INTEGER)")
            setup.execute("INSERT INTO payload VALUES (1)")

        holder = (
            "import sqlite3, time\n"
            f"c = sqlite3.connect({str(src)!r})\n"
            "c.execute('BEGIN EXCLUSIVE')\n"
            "c.execute('INSERT INTO payload VALUES (2)')\n"
            "print('LOCKED', flush=True)\n"
            "time.sleep(0.5)\n"
            "c.commit()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", holder],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout is not None
            assert proc.stdout.readline().strip() == "LOCKED"
            assert _safe_copy_db(src, dst, timeout_seconds=3.0) is True
            assert proc.wait(timeout=3.0) == 0, proc.stderr.read() if proc.stderr else ""
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        with sqlite3.connect(dst) as copied:
            rows = copied.execute("SELECT value FROM payload ORDER BY value").fetchall()
        assert rows == [(1,), (2,)]

    def test_aborts_when_source_remains_busy_past_deadline(
        self, tmp_path, monkeypatch
    ):
        from hermes_cli import backup as backup_mod

        src = tmp_path / "locked.db"
        dst = tmp_path / "copy.db"
        src.touch()
        dst.write_bytes(b"partial")

        clock = iter((99.0, 100.0, 100.5, 101.1))

        class FakeSourceConnection:
            def execute(self, query):
                assert query == "PRAGMA journal_mode"

                class Result:
                    @staticmethod
                    def fetchone():
                        return ("delete",)

                return Result()

            def backup(self, _destination, *, pages, progress, sleep):
                assert pages > 0
                assert sleep > 0
                progress(sqlite3.SQLITE_BUSY, 0, 1)
                progress(sqlite3.SQLITE_BUSY, 0, 1)

            def close(self):
                pass

        destination_closed = []

        class FakeDestinationConnection:
            def close(self):
                destination_closed.append(True)

        connections = iter((FakeSourceConnection(), FakeDestinationConnection()))
        real_unlink = Path.unlink

        def assert_closed_before_unlink(path, *args, **kwargs):
            assert destination_closed
            return real_unlink(path, *args, **kwargs)

        connect_calls = []

        def fake_connect(*args, **kwargs):
            connect_calls.append((args, kwargs))
            return next(connections)

        monkeypatch.setattr(backup_mod.sqlite3, "connect", fake_connect)
        monkeypatch.setattr(backup_mod.time, "monotonic", lambda: next(clock))
        monkeypatch.setattr(Path, "unlink", assert_closed_before_unlink)

        assert backup_mod._safe_copy_db(src, dst, timeout_seconds=1.0) is False
        assert connect_calls[0][1]["timeout"] == 0.0
        assert not dst.exists()


    def test_locked_source_fails_fast_not_hang(self, tmp_path):
        import subprocess
        import sys
        import time

        from hermes_cli.backup import _safe_copy_db
        src = tmp_path / "locked.db"
        dst = tmp_path / "copy.db"

        conn = sqlite3.connect(str(src))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        conn.close()

        # Hold an EXCLUSIVE transaction in a separate process. POSIX file
        # locks only conflict across processes, so an in-process connection
        # cannot reproduce the "database is locked" condition.
        holder = (
            "import sqlite3, time\n"
            f"c = sqlite3.connect({str(src)!r})\n"
            "c.execute('BEGIN EXCLUSIVE')\n"
            "print('LOCKED', flush=True)\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", holder],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout is not None
            assert proc.stdout.readline().strip() == "LOCKED"
            started = time.monotonic()
            result = _safe_copy_db(src, dst, timeout_seconds=0.25)
            elapsed = time.monotonic() - started
            assert result is False
            # The regression this guards against is backup() retrying SQLITE_BUSY forever.
            assert elapsed < 5
        finally:
            proc.kill()
            proc.wait()


    def test_is_zeroed_sqlite_file_detects_nul_header(self, tmp_path):
        from hermes_cli.backup import is_zeroed_sqlite_file
        p = tmp_path / "state.db"
        p.write_bytes(bytes(4096))  # all NULs
        assert is_zeroed_sqlite_file(p) is True

class TestQuickSnapshot:
    @pytest.fixture
    def hermes_home(self, tmp_path):
        """Create a fake HERMES_HOME with critical state files."""
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text("model:\n  provider: openrouter\n")
        (home / ".env").write_text("OPENROUTER_API_KEY=test-key-123\n")
        (home / "auth.json").write_text('{"providers": {}}\n')
        (home / "channel_aliases.json").write_text(
            '{"whatsapp": {"120363408391911677@g.us": "general"}}\n'
        )
        (home / "cron").mkdir()
        (home / "cron" / "jobs.json").write_text('{"jobs": []}\n')

        # Real SQLite database
        db_path = home / "state.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, data TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('s1', 'hello world')")
        conn.commit()
        conn.close()
        return home



    def test_state_db_safely_copied(self, hermes_home):
        from hermes_cli.backup import create_quick_snapshot
        snap_id = create_quick_snapshot(hermes_home=hermes_home)
        db_copy = hermes_home / "state-snapshots" / snap_id / "state.db"
        assert db_copy.exists()
        conn = sqlite3.connect(str(db_copy))
        rows = conn.execute("SELECT * FROM sessions").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0] == ("s1", "hello world")

    def test_failed_state_db_copy_is_loud(self, hermes_home, monkeypatch, capsys):
        """#68474: unreadable state.db must not look like a silent success."""
        from hermes_cli import backup as backup_mod

        def boom(src, dst):
            return False

        monkeypatch.setattr(backup_mod, "_safe_copy_db", boom)
        snap_id = backup_mod.create_quick_snapshot(hermes_home=hermes_home)
        # Other small files still snapshot; the failed DB is recorded, not silently dropped.
        assert snap_id
        manifest = (hermes_home / "state-snapshots" / snap_id / "manifest.json")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert "state.db" not in data.get("files", {})
        assert "state.db" in data.get("failed_dbs", [])

    def test_corrupt_snapshot_db_is_not_restored(self, hermes_home, caplog):
        """A published snapshot with a damaged SQLite member is not treated as recoverable."""
        from hermes_cli import backup as backup_mod
        from hermes_cli.backup import create_quick_snapshot, restore_quick_snapshot

        snap_id = create_quick_snapshot(hermes_home=hermes_home)
        snap_db = hermes_home / "state-snapshots" / snap_id / "state.db"
        snap_db.write_bytes(b"not sqlite")
        live_db = hermes_home / "state.db"
        before = live_db.read_bytes()

        assert restore_quick_snapshot(snap_id, hermes_home=hermes_home) is False
        assert live_db.read_bytes() == before
        assert any("Snapshot content verification failed" in record.message
                   for record in caplog.records)


    def test_restore_state_db_live_connection(self, hermes_home):
        """Restoring state.db must update data visible through a live connection.

        Regression test for #65942: when state.db is open with a live SQLite
        connection (as happens with the gateway, dashboard, or another CLI
        session), the restore must write pages through the backup API so the
        live connection sees the restored data instead of stale cached pages
        from a replaced inode.
        """
        from hermes_cli.backup import create_quick_snapshot, restore_quick_snapshot
        snap_id = create_quick_snapshot(hermes_home=hermes_home)

        # Open a live connection (simulating gateway/dashboard).
        live_conn = sqlite3.connect(str(hermes_home / "state.db"))
        live_conn.execute("PRAGMA journal_mode=wal")
        # Insert data AFTER the snapshot — this is what must be reverted.
        live_conn.execute("INSERT INTO sessions VALUES ('s2', 'new-data')")
        live_conn.commit()

        rows_before = live_conn.execute("SELECT * FROM sessions").fetchall()
        assert len(rows_before) == 2

        # Restore — the live connection stays open during restore.
        result = restore_quick_snapshot(snap_id, hermes_home=hermes_home)
        assert result is True

        # The live connection must see the restored (single-row) state.
        # A fresh connection would trivially work; the live one is the test.
        rows_after = live_conn.execute("SELECT * FROM sessions").fetchall()
        live_conn.close()
        assert len(rows_after) == 1, (
            f"Live connection still sees {len(rows_after)} rows after restore "
            f"(expected 1); the extra row 's2' should have been reverted."
        )














    def test_snapshot_includes_pairing_directories(self, hermes_home):
        """Pairing JSONs live outside state.db — snapshot must capture them
        recursively (generic + per-platform) so approved-user lists survive
        disasters like #15733."""
        from hermes_cli.backup import create_quick_snapshot

        # Generic pairing store (new location)
        (hermes_home / "platforms" / "pairing").mkdir(parents=True)
        (hermes_home / "platforms" / "pairing" / "telegram-approved.json").write_text(
            '{"12345": {"user_name": "alice"}}'
        )
        (hermes_home / "platforms" / "pairing" / "discord-approved.json").write_text(
            '{"67890": {"user_name": "bob"}}'
        )
        # Legacy pairing store (old location)
        (hermes_home / "pairing").mkdir()
        (hermes_home / "pairing" / "matrix-approved.json").write_text(
            '{"@charlie:server": {"user_name": "charlie"}}'
        )
        # Feishu's separate JSON
        (hermes_home / "feishu_comment_pairing.json").write_text(
            '{"doc_abc": {"allow_from": ["user_xyz"]}}'
        )

        snap_id = create_quick_snapshot(hermes_home=hermes_home)
        assert snap_id is not None

        snap_dir = hermes_home / "state-snapshots" / snap_id
        assert (snap_dir / "platforms" / "pairing" / "telegram-approved.json").exists()
        assert (snap_dir / "platforms" / "pairing" / "discord-approved.json").exists()
        assert (snap_dir / "pairing" / "matrix-approved.json").exists()
        assert (snap_dir / "feishu_comment_pairing.json").exists()

        with open(snap_dir / "manifest.json") as f:
            meta = json.load(f)
        files = meta["files"]
        assert "platforms/pairing/telegram-approved.json" in files
        assert "platforms/pairing/discord-approved.json" in files
        assert "pairing/matrix-approved.json" in files
        assert "feishu_comment_pairing.json" in files



# ---------------------------------------------------------------------------
# Pre-update backup (hermes update safety net)
# ---------------------------------------------------------------------------

    # -- security: path traversal regression coverage -----------------------
    # Per @egilewski audit on PR #9217: restore_quick_snapshot must reject
    # malicious snapshot_id values (the directory selector) AND malicious
    # rel paths inside the manifest (the per-file selector). Both surfaces
    # need explicit regression tests because they validate independent
    # traversal vectors.



    def test_oversized_db_suppresses_pruning(self, hermes_home, capsys):
        """#68805: an oversized state.db skipped for size must suppress
        pruning so the older complete snapshot (containing the only
        recoverable database) is preserved.

        Reproduces the reviewer's scenario: keep=1 + a state.db exceeding
        the size cap → the new snapshot omits state.db, failed_dbs stays
        empty (the file wasn't unreadable, just too large), and without
        tracking oversized_skipped the older complete snapshot would be
        pruned — losing the only recovery copy.
        """
        import json
        from hermes_cli.backup import create_quick_snapshot, list_quick_snapshots

        # First snapshot: complete (state.db is small, under any cap)
        first_id = create_quick_snapshot(label="complete", hermes_home=hermes_home)
        assert first_id is not None
        first_dir = hermes_home / "state-snapshots" / first_id
        assert (first_dir / "state.db").exists()

        _advance_backup_clock()

        # Second snapshot: state.db exceeds the 1024-byte cap → skipped for
        # size, but small config files (32-54 bytes) still land in the manifest.
        second_id = create_quick_snapshot(
            label="oversized", hermes_home=hermes_home, max_file_size=1024, keep=1
        )
        assert second_id is not None
        second_dir = hermes_home / "state-snapshots" / second_id
        assert not (second_dir / "state.db").exists()

        # Manifest must record the oversized skip
        with open(second_dir / "manifest.json") as f:
            meta = json.load(f)
        assert "state.db" in meta.get("oversized_skipped", [])

        # CRITICAL: the first (complete) snapshot must survive pruning
        # because the second snapshot is incomplete (oversized state.db).
        all_snaps = list_quick_snapshots(limit=100, hermes_home=hermes_home)
        snap_ids = {s["id"] for s in all_snaps}
        assert first_id in snap_ids, (
            f"Complete snapshot {first_id} was pruned by an incomplete "
            f"(oversized) snapshot — the recovery copy was lost!"
        )
        assert second_id in snap_ids

class TestQuickSnapshotProjectsKanban:
    """Regression for #52889: projects.db / kanban.db must survive an upgrade.

    Both are per-profile user-created stores outside the git checkout. If they
    are not in the pre-update snapshot, the post-update ``CREATE TABLE IF NOT
    EXISTS`` runs against a missing file and every project / board row is lost.
    """

    @pytest.fixture
    def hermes_home(self, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir()
        # Minimal critical file so the snapshot is non-empty.
        (home / "config.yaml").write_text("model:\n  provider: openrouter\n")

        for name, table, row in (
            ("projects.db", "projects", ("p1", "demo")),
            ("kanban.db", "tasks", ("t1", "todo")),
        ):
            conn = sqlite3.connect(str(home / name))
            conn.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, data TEXT)")
            conn.execute(f"INSERT INTO {table} VALUES (?, ?)", row)
            conn.commit()
            conn.close()
        return home



    def test_non_default_kanban_board_snapshotted(self, hermes_home):
        """#52889 completeness: non-default boards live at
        <root>/kanban/boards/<slug>/kanban.db, not <root>/kanban.db. The
        ``kanban/boards`` dir entry must capture them too, or multi-board
        users still lose every board except ``default`` on upgrade."""
        from hermes_cli.backup import create_quick_snapshot, restore_quick_snapshot

        board_dir = hermes_home / "kanban" / "boards" / "work"
        board_dir.mkdir(parents=True)
        conn = sqlite3.connect(str(board_dir / "kanban.db"))
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, data TEXT)")
        conn.execute("INSERT INTO tasks VALUES (?, ?)", ("w1", "ship"))
        conn.commit()
        conn.close()

        snap_id = create_quick_snapshot(hermes_home=hermes_home)
        copy = (
            hermes_home / "state-snapshots" / snap_id
            / "kanban" / "boards" / "work" / "kanban.db"
        )
        assert copy.exists(), "non-default board kanban.db was not snapshotted"

        # Simulate the upgrade wiping the board, then restore it.
        conn = sqlite3.connect(str(board_dir / "kanban.db"))
        conn.execute("DELETE FROM tasks")
        conn.commit()
        conn.close()

        assert restore_quick_snapshot(snap_id, hermes_home=hermes_home) is True
        conn = sqlite3.connect(str(board_dir / "kanban.db"))
        rows = conn.execute("SELECT * FROM tasks").fetchall()
        conn.close()
        assert rows == [("w1", "ship")]



    def test_board_db_copied_wal_safely(self, hermes_home, monkeypatch):
        """#52889 W2: a non-default board's .db (dir-branch) must go through the
        WAL-safe _safe_copy_db, not a raw shutil.copy2, so an open WAL doesn't
        produce an inconsistent copy."""
        import hermes_cli.backup as bk
        from hermes_cli.backup import create_quick_snapshot

        board = hermes_home / "kanban" / "boards" / "work"
        board.mkdir(parents=True)
        conn = sqlite3.connect(str(board / "kanban.db"))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, data TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('w1', 'ship')")
        conn.commit()
        conn.close()

        called = {"db": []}
        real = bk._safe_copy_db

        def _spy(src, dst):
            called["db"].append(str(src))
            return real(src, dst)

        monkeypatch.setattr(bk, "_safe_copy_db", _spy)
        snap_id = create_quick_snapshot(hermes_home=hermes_home)
        # The board db was copied via _safe_copy_db (not raw copy).
        assert any(s.endswith("boards/work/kanban.db") for s in called["db"]), called["db"]
        copy = hermes_home / "state-snapshots" / snap_id / "kanban" / "boards" / "work" / "kanban.db"
        rows = sqlite3.connect(str(copy)).execute("SELECT * FROM tasks").fetchall()
        assert rows == [("w1", "ship")]

class TestFailedZipMemberRecovery:
    def test_automatic_backup_omits_crc_valid_partial_member(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text("model: test\n")
        (hermes_home / "flaky.bin").write_bytes(os.urandom(300_000))
        archive = tmp_path / "automatic.zip"
        _fail_zip_write_after(monkeypatch, "flaky.bin")

        from hermes_cli.backup import _write_full_zip_backup

        result = _write_full_zip_backup(archive, hermes_home)
        assert not archive.exists()
        salvage = tmp_path / "automatic.incomplete.zip"
        assert result == salvage
        # A streaming reader scans local headers, so the dropped member's bytes must be gone too.
        assert b"flaky.bin" not in salvage.read_bytes()
        with zipfile.ZipFile(salvage) as zf:
            assert "flaky.bin" not in zf.namelist()
            assert zf.read("config.yaml") == b"model: test\n"
            assert zf.testzip() is None

    def test_incomplete_pre_update_backup_does_not_rotate_last_complete(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text("model: test\n")

        from hermes_cli.backup import create_pre_update_backup

        good = create_pre_update_backup(hermes_home=hermes_home, keep=2)
        assert good is not None and good.exists()

        real_write = zipfile.ZipFile.write
        (hermes_home / "flaky.bin").write_bytes(os.urandom(300_000))
        _fail_zip_write_after(monkeypatch, "flaky.bin")
        for _ in range(2):
            _advance_backup_clock()
            result = create_pre_update_backup(hermes_home=hermes_home, keep=2)
            assert result is not None and result.name.endswith(".incomplete.zip")

        assert good.exists(), "an incomplete generation rotated out the last complete backup"
        backup_dir = hermes_home / "backups"
        salvages = list(backup_dir.glob("pre-update-*.incomplete.zip"))
        assert len(salvages) == 1, "repeated incomplete runs must not pile up"
        with zipfile.ZipFile(salvages[0]) as zf:
            assert "flaky.bin" not in zf.namelist()
            assert zf.read("config.yaml") == b"model: test\n"
            assert zf.testzip() is None

        # Salvage archives must not count toward retention on the next complete run.
        monkeypatch.setattr(zipfile.ZipFile, "write", real_write)
        (hermes_home / "flaky.bin").unlink()
        _advance_backup_clock()
        newest = create_pre_update_backup(hermes_home=hermes_home, keep=2)
        assert newest is not None and newest.exists()
        assert good.exists(), "a complete run pruned complete backups in favour of salvage"
        assert len(list(backup_dir.glob("pre-update-*.incomplete.zip"))) == 1

class TestPreUpdateBackup:
    """Tests for create_pre_update_backup — the auto-backup ``hermes update``
    runs before touching anything."""


    @pytest.fixture
    def hermes_home(self, tmp_path):
        root = tmp_path / ".hermes"
        root.mkdir()
        _make_hermes_tree(root)
        return root


    def test_backup_contents_match_full_backup(self, hermes_home):
        """Pre-update backup should include the same user data that
        ``hermes backup`` would, and should exclude the same directories."""
        from hermes_cli.backup import create_pre_update_backup
        out = create_pre_update_backup(hermes_home=hermes_home)
        assert out is not None
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
        # User data present
        assert "config.yaml" in names
        assert ".env" in names
        assert "sessions/abc123.json" in names
        assert "skills/my-skill/SKILL.md" in names
        assert "profiles/coder/config.yaml" in names
        # hermes-agent repo excluded
        assert not any(n.startswith("hermes-agent/") for n in names)
        # __pycache__ excluded
        assert not any("__pycache__" in n for n in names)
        # pid files excluded
        assert "gateway.pid" not in names

    def test_pre_update_zip_does_not_nest_the_pre_update_snapshot(self, hermes_home):
        """``hermes update`` in ``full`` mode takes the quick snapshot *before*
        the full zip, so the zip walk sees the snapshot it just made. It must
        skip it — otherwise every pre-update zip ships state.db twice."""
        from hermes_cli.backup import (
            _QUICK_SNAPSHOTS_DIR,
            create_pre_update_backup,
            create_quick_snapshot,
        )
        with sqlite3.connect(hermes_home / "state.db") as conn:
            conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")

        snap_id = create_quick_snapshot(label="pre-update", hermes_home=hermes_home)
        assert snap_id and (hermes_home / _QUICK_SNAPSHOTS_DIR / snap_id / "state.db").exists()

        out = create_pre_update_backup(hermes_home=hermes_home)
        assert out is not None
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        assert "state.db" in names
        assert not any(n.startswith(_QUICK_SNAPSHOTS_DIR + "/") for n in names), names


    def test_rotation_keeps_only_n(self, hermes_home):
        """After more than ``keep`` backups are created, older ones are
        pruned automatically."""
        from hermes_cli.backup import create_pre_update_backup

        created = []
        for _ in range(5):
            out = create_pre_update_backup(hermes_home=hermes_home, keep=3)
            created.append(out)
            _advance_backup_clock()

        remaining = sorted(
            p.name for p in (hermes_home / "backups").iterdir()
            if p.name.startswith("pre-update-")
        )
        assert len(remaining) == 3
        # Oldest two should have been pruned
        assert created[0].name not in remaining
        assert created[1].name not in remaining
        # Newest three should remain
        assert created[4].name in remaining






    def test_skips_symlinked_files(self, hermes_home, tmp_path):
        """Pre-update backups must not dereference symlinks outside HERMES_HOME."""
        from hermes_cli.backup import create_pre_update_backup

        outside = tmp_path / "outside-secret.txt"
        outside.write_text("outside secret\n")
        _symlink_file_or_skip(hermes_home / "skills" / "outside-link.txt", outside)

        out = create_pre_update_backup(hermes_home=hermes_home)
        assert out is not None
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            assert "skills/outside-link.txt" not in names
            assert all(zf.read(name) != b"outside secret\n" for name in names)

class TestRunPreUpdateBackup:
    """Tests for the ``_run_pre_update_backup`` wrapper in main.py —
    covers the consolidated off/quick/full mode gate, CLI flags, and
    user-facing output."""

    @pytest.fixture
    def hermes_home(self, tmp_path, monkeypatch):
        root = tmp_path / ".hermes"
        root.mkdir()
        _make_hermes_tree(root)
        # Point HERMES_HOME at the temp dir so config + backup paths resolve here
        monkeypatch.setenv("HERMES_HOME", str(root))
        # Make Path.home() point at tmp_path for anything that uses it
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # Config reads resolve HERMES_HOME dynamically and their caches are
        # keyed by config path. Do not remove shared modules from sys.modules:
        # other test modules may retain imports from the existing module object.
        return root

    @staticmethod
    def _set_mode(hermes_home, value):
        import hermes_yaml as yaml
        (hermes_home / "config.yaml").write_text(yaml.safe_dump({
            "_config_version": 22,
            "updates": {"pre_update_backup": value},
        }))

    @staticmethod
    def _zips(hermes_home):
        d = hermes_home / "backups"
        return list(d.glob("pre-update-*.zip")) if d.exists() else []

    @staticmethod
    def _snaps(hermes_home):
        d = hermes_home / "state-snapshots"
        return [p for p in d.iterdir() if p.is_dir()] if d.exists() else []




    def test_config_off_disables_everything_silently(self, hermes_home, capsys):
        """pre_update_backup: off — an explicit opt-out disables the quick
        snapshot too (it previously ran unconditionally), with no output."""
        self._set_mode(hermes_home, "off")
        from hermes_cli.update_cmd import _run_pre_update_backup
        snap_id = _run_pre_update_backup(Namespace(no_backup=False, backup=False))
        out = capsys.readouterr().out
        assert snap_id is None
        assert out == ""
        assert not self._snaps(hermes_home)
        assert not self._zips(hermes_home)



    def test_config_full_mode(self, hermes_home, capsys):
        self._set_mode(hermes_home, "full")
        from hermes_cli.update_cmd import _run_pre_update_backup
        snap_id = _run_pre_update_backup(Namespace(no_backup=False, backup=False))
        assert snap_id is not None
        assert len(self._zips(hermes_home)) == 1

class TestPreMigrationBackup:
    """Tests for create_pre_migration_backup — the auto-backup
    ``hermes import-agent openclaw`` runs before mutating ~/.hermes/."""

    @pytest.fixture
    def hermes_home(self, tmp_path):
        root = tmp_path / ".hermes"
        root.mkdir()
        _make_hermes_tree(root)
        return root


    def test_restorable_with_hermes_import(self, hermes_home, tmp_path):
        """The zip produced by pre-migration backup must be a valid Hermes
        backup — `hermes import` should accept it."""
        from hermes_cli.backup import create_pre_migration_backup, _validate_backup_zip
        out = create_pre_migration_backup(hermes_home=hermes_home)
        assert out is not None
        with zipfile.ZipFile(out) as zf:
            valid, _reason = _validate_backup_zip(zf)
        assert valid, "pre-migration zip failed _validate_backup_zip"




    def test_does_not_touch_pre_update_backups(self, hermes_home):
        """Pre-migration rotation must only prune pre-migration-*.zip files,
        leaving pre-update-*.zip backups untouched."""
        from hermes_cli.backup import create_pre_update_backup, create_pre_migration_backup
        update_backup = create_pre_update_backup(hermes_home=hermes_home, keep=5)
        assert update_backup is not None and update_backup.exists()
        # Spin up a lot of migration backups with keep=1
        for _ in range(3):
            out = create_pre_migration_backup(hermes_home=hermes_home, keep=1)
            assert out is not None
            _advance_backup_clock()
        # Update backup must still be there
        assert update_backup.exists(), "pre-migration rotation wrongly pruned the pre-update backup"

class TestRestoreCronJobsIfEmptied:
    """`hermes update` config migration can leave cron/jobs.json valid-but-empty,
    silently dropping every scheduled job. `restore_cron_jobs_if_emptied` is the
    post-migration safety net that restores from the pre-update snapshot."""

    @staticmethod
    def _seed_jobs(path: Path, jobs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"jobs": jobs}))

    def _make_snapshot(self, hermes_home: Path, label="pre-update"):
        from hermes_cli.backup import create_quick_snapshot
        return create_quick_snapshot(label=label, hermes_home=hermes_home, keep=5)

    def test_restores_when_emptied_after_migration(self, tmp_path):
        from hermes_cli.backup import restore_cron_jobs_if_emptied
        hermes_home = tmp_path / ".hermes"
        jobs_path = hermes_home / "cron" / "jobs.json"
        # Pre-update: 3 real jobs.
        self._seed_jobs(jobs_path, [{"id": "a"}, {"id": "b"}, {"id": "c"}])
        snap_id = self._make_snapshot(hermes_home)
        assert snap_id

        # Migration silently empties the file (valid JSON, zero jobs).
        jobs_path.write_text(json.dumps({"jobs": []}))

        result = restore_cron_jobs_if_emptied(snap_id, hermes_home=hermes_home)
        assert result is not None
        assert result["restored"] is True
        assert result["job_count"] == 3
        assert result["snapshot_id"] == snap_id

        # The live file now has the jobs back.
        restored = json.loads(jobs_path.read_text())
        assert len(restored["jobs"]) == 3


    def test_total_loss_preserves_snapshot_document_metadata(self, tmp_path):
        from hermes_cli.backup import restore_cron_jobs_if_emptied
        hermes_home = tmp_path / ".hermes"
        jobs_path = hermes_home / "cron" / "jobs.json"
        expected = {
            "jobs": [{"id": "a", "prompt": "report"}],
            "updated_at": "2026-01-01T00:00:00+00:00",
            "format": "cron-v1",
        }
        jobs_path.parent.mkdir(parents=True, exist_ok=True)
        jobs_path.write_text(json.dumps(expected), encoding="utf-8")
        snap_id = self._make_snapshot(hermes_home)
        assert snap_id

        jobs_path.write_text('{"jobs": []}', encoding="utf-8")

        assert restore_cron_jobs_if_emptied(snap_id, hermes_home=hermes_home) is not None
        assert json.loads(jobs_path.read_text(encoding="utf-8")) == expected

    def test_partial_restore_drops_malformed_live_entries(self, tmp_path):
        from hermes_cli.backup import restore_cron_jobs_if_emptied
        hermes_home = tmp_path / ".hermes"
        jobs_path = hermes_home / "cron" / "jobs.json"
        jobs_path.parent.mkdir(parents=True, exist_ok=True)
        jobs_path.write_text(json.dumps({
            "jobs": [
                {"id": "a", "prompt": "one"}, {"id": "b", "prompt": "two"},
                {"id": "c", "prompt": "three"},
            ],
        }), encoding="utf-8")
        snap_id = self._make_snapshot(hermes_home)
        assert snap_id

        jobs_path.write_text(json.dumps({
            "jobs": [42, {"id": "a", "prompt": "one"}], "format": "cron-v1",
        }), encoding="utf-8")

        assert restore_cron_jobs_if_emptied(snap_id, hermes_home=hermes_home) is not None
        restored = json.loads(jobs_path.read_text(encoding="utf-8"))
        assert all(isinstance(job, dict) for job in restored["jobs"])
        assert sorted(job["id"] for job in restored["jobs"]) == ["a", "b", "c"]
        assert restored["format"] == "cron-v1"

    @pytest.mark.parametrize("malformed", [42, {}, {"id": ""}])
    def test_equal_raw_count_with_malformed_live_entry_still_restores(self, tmp_path, malformed):
        from hermes_cli.backup import restore_cron_jobs_if_emptied
        hermes_home = tmp_path / ".hermes"
        jobs_path = hermes_home / "cron" / "jobs.json"
        jobs_path.parent.mkdir(parents=True, exist_ok=True)
        jobs_path.write_text(json.dumps({"jobs": [{"id": "a", "prompt": "one"}]}), encoding="utf-8")
        snap_id = self._make_snapshot(hermes_home)
        assert snap_id
        jobs_path.write_text(json.dumps({"jobs": [malformed]}), encoding="utf-8")

        assert restore_cron_jobs_if_emptied(snap_id, hermes_home=hermes_home) is not None
        restored = json.loads(jobs_path.read_text(encoding="utf-8"))
        assert "a" in [job.get("id") for job in restored["jobs"] if isinstance(job, dict)]

    def test_restore_keeps_job_created_after_its_read(self, tmp_path, monkeypatch):
        """A sibling create landing after restore read the live file survives the write."""
        from cron import jobs as cron_jobs
        from hermes_cli.backup import restore_cron_jobs_if_emptied
        hermes_home = tmp_path / ".hermes"
        jobs_path = hermes_home / "cron" / "jobs.json"
        jobs_path.parent.mkdir(parents=True, exist_ok=True)
        jobs_path.write_text(json.dumps({"jobs": [
            {"id": "a", "prompt": "one"}, {"id": "b", "prompt": "two"},
            {"id": "c", "prompt": "three"},
        ]}), encoding="utf-8")
        snap_id = self._make_snapshot(hermes_home)
        assert snap_id
        jobs_path.write_text(json.dumps({"jobs": [{"id": "a", "prompt": "one"}]}), encoding="utf-8")

        real_load = cron_jobs.load_jobs

        def load_then_sibling_create(*args, **kwargs):
            loaded = real_load(*args, **kwargs)
            doc = json.loads(jobs_path.read_text(encoding="utf-8"))
            doc["jobs"].append({"id": "sibling", "prompt": "new"})
            jobs_path.write_text(json.dumps(doc), encoding="utf-8")
            return loaded

        monkeypatch.setattr(cron_jobs, "load_jobs", load_then_sibling_create)

        assert restore_cron_jobs_if_emptied(snap_id, hermes_home=hermes_home) is not None
        restored = json.loads(jobs_path.read_text(encoding="utf-8"))
        assert sorted(job["id"] for job in restored["jobs"]) == ["a", "b", "c", "sibling"]


    def test_non_utf8_journal_does_not_stop_restore(self, tmp_path):
        from hermes_cli.backup import restore_cron_jobs_if_emptied
        hermes_home = tmp_path / ".hermes"
        jobs_path = hermes_home / "cron" / "jobs.json"
        self._seed_jobs(jobs_path, [{"id": "a"}, {"id": "b"}])
        snap_id = self._make_snapshot(hermes_home)
        assert snap_id

        (hermes_home / "cron" / "removed_jobs.jsonl").write_bytes(b"{\xff\n")
        jobs_path.write_text(json.dumps({"jobs": []}))

        result = restore_cron_jobs_if_emptied(snap_id, hermes_home=hermes_home)

        assert result is not None
        assert result["restored"] is True
        assert result["job_count"] == 2
        assert {job["id"] for job in json.loads(jobs_path.read_text())["jobs"]} == {"a", "b"}


    def test_append_after_corrupt_journal_excludes_deleted_job_from_restore(self, tmp_path):
        from cron.jobs import remove_job, use_cron_store
        from hermes_cli.backup import restore_cron_jobs_if_emptied
        hermes_home = tmp_path / ".hermes"
        jobs_path = hermes_home / "cron" / "jobs.json"
        self._seed_jobs(jobs_path, [{"id": "a"}, {"id": "b"}])
        snap_id = self._make_snapshot(hermes_home)
        assert snap_id

        removed_file = hermes_home / "cron" / "removed_jobs.jsonl"
        removed_file.write_bytes(b"{\xff\n")
        with use_cron_store(hermes_home):
            assert remove_job("a") is True
        jobs_path.write_text(json.dumps({"jobs": []}))

        result = restore_cron_jobs_if_emptied(snap_id, hermes_home=hermes_home)

        assert result is not None
        assert result["restored"] is True
        assert result["job_count"] == 1
        assert {job["id"] for job in json.loads(jobs_path.read_text())["jobs"]} == {"b"}


    def test_restores_when_partial_job_loss(self, tmp_path):
        """Desktop scheduler overwrites jobs.json with its own small set,
        losing tool-created crons while keeping desktop-tracked ones."""
        from hermes_cli.backup import restore_cron_jobs_if_emptied
        hermes_home = tmp_path / ".hermes"
        jobs_path = hermes_home / "cron" / "jobs.json"
        # Pre-update: 19 jobs (18 tool-created + 1 desktop watchdog).
        self._seed_jobs(
            jobs_path,
            [{"id": f"job-{i}"} for i in range(19)],
        )
        snap_id = self._make_snapshot(hermes_home)
        assert snap_id

        # Desktop scheduler overwrites with only its own 1 job.
        jobs_path.write_text(json.dumps({"jobs": [{"id": "desktop-watchdog"}]}))

        result = restore_cron_jobs_if_emptied(snap_id, hermes_home=hermes_home)
        assert result is not None
        assert result["restored"] is True
        assert result["job_count"] == 19

        # The live desktop-owned job survives, and all 19 snapshot jobs are restored.
        restored = json.loads(jobs_path.read_text())
        assert len(restored["jobs"]) == 20
        assert "desktop-watchdog" in {job["id"] for job in restored["jobs"]}

    def test_does_not_resurrect_job_removed_after_snapshot(self, tmp_path):
        from cron.jobs import remove_job, use_cron_store
        from hermes_cli.backup import restore_cron_jobs_if_emptied
        hermes_home = tmp_path / ".hermes"
        jobs_path = hermes_home / "cron" / "jobs.json"
        self._seed_jobs(jobs_path, [{"id": "a"}, {"id": "b"}, {"id": "c"}])
        snap_id = self._make_snapshot(hermes_home)
        assert snap_id

        with use_cron_store(hermes_home):
            assert remove_job("a") is True

        result = restore_cron_jobs_if_emptied(snap_id, hermes_home=hermes_home)
        assert result is None
        assert {job["id"] for job in json.loads(jobs_path.read_text())["jobs"]} == {"b", "c"}

    def test_restores_missing_job_without_reverting_survivor_change(self, tmp_path):
        from cron.jobs import remove_job, use_cron_store
        from hermes_cli.backup import restore_cron_jobs_if_emptied
        hermes_home = tmp_path / ".hermes"
        jobs_path = hermes_home / "cron" / "jobs.json"
        self._seed_jobs(
            jobs_path,
            [{"id": "a", "last_run_at": None}, {"id": "b"}, {"id": "c"}],
        )
        snap_id = self._make_snapshot(hermes_home)
        assert snap_id

        with use_cron_store(hermes_home):
            assert remove_job("c") is True
        jobs_path.write_text(json.dumps({"jobs": [{"id": "a", "last_run_at": "new"}]}))

        result = restore_cron_jobs_if_emptied(snap_id, hermes_home=hermes_home)
        assert result is not None
        assert result["job_count"] == 1
        restored = {job["id"]: job for job in json.loads(jobs_path.read_text())["jobs"]}
        assert restored["a"]["last_run_at"] == "new"
        assert set(restored) == {"a", "b"}

    def test_delete_before_snapshot_outside_tolerance_does_not_block_restore(self, tmp_path):
        from hermes_cli.backup import restore_cron_jobs_if_emptied
        hermes_home = tmp_path / ".hermes"
        jobs_path = hermes_home / "cron" / "jobs.json"
        self._seed_jobs(jobs_path, [{"id": "a"}, {"id": "b"}])
        (hermes_home / "cron" / "removed_jobs.jsonl").parent.mkdir(parents=True, exist_ok=True)
        (hermes_home / "cron" / "removed_jobs.jsonl").write_text(
            '{"id":"a","removed_at":"2000-01-01T00:00:00+00:00"}\n')
        snap_id = self._make_snapshot(hermes_home)
        jobs_path.write_text(json.dumps({"jobs": []}))
        result = restore_cron_jobs_if_emptied(snap_id, hermes_home=hermes_home)
        assert result is not None
        assert result["job_count"] == 2

    def test_manifest_without_timestamp_treats_all_deletes_as_deliberate(self, tmp_path):
        from cron.jobs import remove_job, use_cron_store
        from hermes_cli.backup import restore_cron_jobs_if_emptied
        hermes_home = tmp_path / ".hermes"
        jobs_path = hermes_home / "cron" / "jobs.json"
        self._seed_jobs(jobs_path, [{"id": "a"}, {"id": "b"}])
        snap_id = self._make_snapshot(hermes_home)
        manifest = hermes_home / "state-snapshots" / snap_id / "manifest.json"
        data = json.loads(manifest.read_text())
        data.pop("timestamp", None)
        data.pop("created_at", None)
        manifest.write_text(json.dumps(data))
        with use_cron_store(hermes_home):
            assert remove_job("a") is True
        jobs_path.write_text(json.dumps({"jobs": []}))
        result = restore_cron_jobs_if_emptied(snap_id, hermes_home=hermes_home)
        assert result is not None
        assert result["job_count"] == 1
        assert {j["id"] for j in json.loads(jobs_path.read_text())["jobs"]} == {"b"}

class TestRestoreCronPromptFieldsIfDegraded:
    """A writer in the update's mutation window replaced every agent-job prompt
    with the job's own name while the count stayed identical — the count-based
    net passed it undetected. `restore_cron_prompt_fields_if_degraded` is the
    field-level net."""

    @staticmethod
    def _seed_jobs(path: Path, jobs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"jobs": jobs}))

    def _make_snapshot(self, hermes_home: Path, label="pre-update"):
        from hermes_cli.backup import create_quick_snapshot
        return create_quick_snapshot(label=label, hermes_home=hermes_home, keep=5)

    def test_restores_prompts_when_count_unchanged_but_clobbered(self, tmp_path):
        """The reported incident: 6 jobs before, 6 after, every prompt == name."""
        from hermes_cli.backup_cron_prompts import restore_cron_prompt_fields_if_degraded
        hermes_home = tmp_path / ".hermes"
        jobs_path = hermes_home / "cron" / "jobs.json"
        self._seed_jobs(jobs_path, [
            {"id": f"job-{i}", "name": f"Morning brief {i}",
             "prompt": f"Read the news and write brief {i}."}
            for i in range(6)
        ])
        snap_id = self._make_snapshot(hermes_home)
        assert snap_id

        # The mutation window clobbers prompts to the name; count unchanged.
        self._seed_jobs(jobs_path, [
            {"id": f"job-{i}", "name": f"Morning brief {i}",
             "prompt": f"Morning brief {i}"}
            for i in range(6)
        ])

        result = restore_cron_prompt_fields_if_degraded(snap_id, hermes_home=hermes_home)
        assert result is not None
        assert result["restored"] is True
        assert result["prompts"] == 6
        assert result["snapshot_id"] == snap_id

        restored = json.loads(jobs_path.read_text())
        for i, job in enumerate(restored["jobs"]):
            assert job["prompt"] == f"Read the news and write brief {i}."
            # Only the prompt was touched — name still the live one.
            assert job["name"] == f"Morning brief {i}"

    def test_blank_prompt_is_restored_too(self, tmp_path):
        """Missing/blank prompt on a live agent job is equally unusable."""
        from hermes_cli.backup_cron_prompts import restore_cron_prompt_fields_if_degraded
        hermes_home = tmp_path / ".hermes"
        jobs_path = hermes_home / "cron" / "jobs.json"
        self._seed_jobs(jobs_path, [{"id": "a", "name": "Cleanup", "prompt": "Clean the tmp dir."}])
        snap_id = self._make_snapshot(hermes_home)
        assert snap_id

        self._seed_jobs(jobs_path, [{"id": "a", "name": "Cleanup", "prompt": ""}])

        result = restore_cron_prompt_fields_if_degraded(snap_id, hermes_home=hermes_home)
        assert result is not None
        assert result["prompts"] == 1
        assert json.loads(jobs_path.read_text())["jobs"][0]["prompt"] == "Clean the tmp dir."

    def test_no_agent_jobs_are_untouched(self, tmp_path):
        """Script jobs have no prompt — never restored, never stomped."""
        from hermes_cli.backup_cron_prompts import restore_cron_prompt_fields_if_degraded
        hermes_home = tmp_path / ".hermes"
        jobs_path = hermes_home / "cron" / "jobs.json"
        self._seed_jobs(jobs_path, [
            {"id": "agent", "name": "Brief", "prompt": "Write the brief."},
            {"id": "script", "name": "Backup", "no_agent": True, "script": "echo hi", "prompt": "Backup"},
        ])
        snap_id = self._make_snapshot(hermes_home)
        assert snap_id

        self._seed_jobs(jobs_path, [
            {"id": "agent", "name": "Brief", "prompt": "Brief"},
            {"id": "script", "name": "Backup", "no_agent": True, "script": "echo hi", "prompt": "Backup"},
        ])

        result = restore_cron_prompt_fields_if_degraded(snap_id, hermes_home=hermes_home)
        assert result is not None
        assert result["prompts"] == 1
        assert result["job_ids"] == ["agent"]
        jobs = json.loads(jobs_path.read_text())["jobs"]
        assert jobs[0]["prompt"] == "Write the brief."
        assert jobs[1]["prompt"] == "Backup"  # untouched

    def test_a_legitimate_user_edit_is_not_stomped(self, tmp_path):
        """Only prompt==name or blank restores — a genuinely different prompt
        is the user's edit during the window, not degradation."""
        from hermes_cli.backup_cron_prompts import restore_cron_prompt_fields_if_degraded
        hermes_home = tmp_path / ".hermes"
        jobs_path = hermes_home / "cron" / "jobs.json"
        self._seed_jobs(jobs_path, [{"id": "a", "name": "Old", "prompt": "Old prompt text."}])
        snap_id = self._make_snapshot(hermes_home)
        assert snap_id

        self._seed_jobs(jobs_path, [{"id": "a", "name": "New name", "prompt": "Deliberately rewritten prompt."}])

        result = restore_cron_prompt_fields_if_degraded(snap_id, hermes_home=hermes_home)
        assert result is None
        assert json.loads(jobs_path.read_text())["jobs"][0]["prompt"] == "Deliberately rewritten prompt."

    def test_a_legit_prompt_equal_to_name_stays(self, tmp_path):
        """A user may legitimately set prompt == name; a blank SNAPSHOT prompt
        means the snapshot has nothing better — restore nothing."""
        from hermes_cli.backup_cron_prompts import restore_cron_prompt_fields_if_degraded
        hermes_home = tmp_path / ".hermes"
        jobs_path = hermes_home / "cron" / "jobs.json"
        # Snapshot job had a blank prompt (hand-edited oddity).
        self._seed_jobs(jobs_path, [{"id": "a", "name": "Ping", "prompt": ""}])
        snap_id = self._make_snapshot(hermes_home)
        assert snap_id

        # Live prompt collapsed to the name — but the snapshot has nothing to give back.
        self._seed_jobs(jobs_path, [{"id": "a", "name": "Ping", "prompt": "Ping"}])

        result = restore_cron_prompt_fields_if_degraded(snap_id, hermes_home=hermes_home)
        assert result is None

    def test_unknown_job_ids_are_left_alone(self, tmp_path):
        """Jobs the snapshot does not know are new since the snapshot — theirs."""
        from hermes_cli.backup_cron_prompts import restore_cron_prompt_fields_if_degraded
        hermes_home = tmp_path / ".hermes"
        jobs_path = hermes_home / "cron" / "jobs.json"
        self._seed_jobs(jobs_path, [{"id": "a", "name": "A", "prompt": "A prompt."}])
        snap_id = self._make_snapshot(hermes_home)
        assert snap_id

        self._seed_jobs(jobs_path, [
            {"id": "a", "name": "A", "prompt": "A prompt."},
            {"id": "new", "name": "New", "prompt": "New"},  # created during the window
        ])

        result = restore_cron_prompt_fields_if_degraded(snap_id, hermes_home=hermes_home)
        assert result is None

    def test_healthy_file_is_untouched(self, tmp_path):
        from hermes_cli.backup_cron_prompts import restore_cron_prompt_fields_if_degraded
        hermes_home = tmp_path / ".hermes"
        jobs_path = hermes_home / "cron" / "jobs.json"
        self._seed_jobs(jobs_path, [{"id": "a", "name": "A", "prompt": "A prompt."}])
        snap_id = self._make_snapshot(hermes_home)
        assert snap_id
        before = jobs_path.read_text()

        result = restore_cron_prompt_fields_if_degraded(snap_id, hermes_home=hermes_home)
        assert result is None
        assert jobs_path.read_text() == before

class TestRestoreConfigModelSettingsIfRewritten:
    """Desktop update/repair cycles have rewritten user-set model.provider /
    model.default and dropped the moa: section (#64160).
    `restore_config_model_settings_if_rewritten` is the post-update safety net
    that restores only the protected keys from the pre-update snapshot."""

    USER_CONFIG = (
        "_config_version: 39\n"
        "model:\n"
        "  provider: custom\n"
        "  default: zyphra/zamba-3-large\n"
        "  base_url: https://api.zyphra.example/v1\n"
        "moa:\n"
        "  enabled: true\n"
        "  presets:\n"
        "    council:\n"
        "      aggregator: {provider: custom, model: zyphra/zamba-3-large}\n"
        "custom_unknown_key:\n"
        "  hello: world\n"
    )

    def _make_snapshot(self, hermes_home: Path, label="pre-update"):
        from hermes_cli.backup import create_quick_snapshot
        return create_quick_snapshot(label=label, hermes_home=hermes_home, keep=5)

    def _seed(self, hermes_home: Path) -> Path:
        hermes_home.mkdir(parents=True, exist_ok=True)
        cfg = hermes_home / "config.yaml"
        cfg.write_text(self.USER_CONFIG, encoding="utf-8")
        return cfg

    def test_restores_rewritten_provider_and_dropped_moa(self, tmp_path):
        import hermes_yaml as yaml
        from hermes_cli.backup import restore_config_model_settings_if_rewritten

        hermes_home = tmp_path / ".hermes"
        cfg = self._seed(hermes_home)
        snap_id = self._make_snapshot(hermes_home)
        assert snap_id

        # The update flow rewrites config.yaml with defaults: provider flips
        # to deepseek, MoA section is gone (the #64160 field report).
        cfg.write_text(
            "_config_version: 39\nmodel:\n  provider: deepseek\n  default: deepseek-chat\n",
            encoding="utf-8",
        )

        result = restore_config_model_settings_if_rewritten(
            snap_id, hermes_home=hermes_home
        )
        assert result is not None
        assert result["restored"] is True
        assert result["snapshot_id"] == snap_id
        assert "model.provider" in result["keys"]
        assert "model.default" in result["keys"]
        assert "moa" in result["keys"]

        after = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert after["model"]["provider"] == "custom"
        assert after["model"]["default"] == "zyphra/zamba-3-large"
        assert after["model"]["base_url"] == "https://api.zyphra.example/v1"
        assert after["moa"]["enabled"] is True
        assert after["moa"]["presets"]["council"]["aggregator"]["model"] == (
            "zyphra/zamba-3-large"
        )

    def test_noop_when_config_untouched(self, tmp_path):
        from hermes_cli.backup import restore_config_model_settings_if_rewritten

        hermes_home = tmp_path / ".hermes"
        cfg = self._seed(hermes_home)
        snap_id = self._make_snapshot(hermes_home)
        before = cfg.read_text(encoding="utf-8")

        result = restore_config_model_settings_if_rewritten(
            snap_id, hermes_home=hermes_home
        )
        assert result is None
        assert cfg.read_text(encoding="utf-8") == before

    def test_preserves_legitimate_update_writes(self, tmp_path):
        """Only protected keys are restored — a version bump or a new section
        the migration legitimately wrote must survive the restore."""
        import hermes_yaml as yaml
        from hermes_cli.backup import restore_config_model_settings_if_rewritten

        hermes_home = tmp_path / ".hermes"
        cfg = self._seed(hermes_home)
        snap_id = self._make_snapshot(hermes_home)

        cfg.write_text(
            "_config_version: 40\n"      # legitimate migration bump
            "new_section:\n  added: true\n"  # legitimate new default
            "model:\n  provider: deepseek\n",  # illegitimate rewrite
            encoding="utf-8",
        )

        result = restore_config_model_settings_if_rewritten(
            snap_id, hermes_home=hermes_home
        )
        assert result is not None
        after = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert after["model"]["provider"] == "custom"          # restored
        assert after["_config_version"] == 40                   # kept
        assert after["new_section"] == {"added": True}          # kept

    def test_noop_when_user_never_set_protected_keys(self, tmp_path):
        """A config that never had model.provider/moa set gets no restore even
        if the update writes those keys fresh — nothing of the user's was lost."""
        from hermes_cli.backup import restore_config_model_settings_if_rewritten

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir(parents=True)
        cfg = hermes_home / "config.yaml"
        cfg.write_text("_config_version: 39\nagent: {}\n", encoding="utf-8")
        snap_id = self._make_snapshot(hermes_home)

        cfg.write_text(
            "_config_version: 39\nmodel:\n  provider: deepseek\n", encoding="utf-8"
        )
        result = restore_config_model_settings_if_rewritten(
            snap_id, hermes_home=hermes_home
        )
        assert result is None

    def test_noop_on_unreadable_live_config(self, tmp_path):
        """An unparseable live config is a different failure the user must see;
        the safety net leaves it alone (mirrors the cron net's posture)."""
        from hermes_cli.backup import restore_config_model_settings_if_rewritten

        hermes_home = tmp_path / ".hermes"
        cfg = self._seed(hermes_home)
        snap_id = self._make_snapshot(hermes_home)

        cfg.write_text(": not [valid yaml", encoding="utf-8")
        result = restore_config_model_settings_if_rewritten(
            snap_id, hermes_home=hermes_home
        )
        assert result is None
        assert cfg.read_text(encoding="utf-8") == ": not [valid yaml"

    def test_noop_without_snapshot_id(self, tmp_path):
        from hermes_cli.backup import restore_config_model_settings_if_rewritten

        assert restore_config_model_settings_if_rewritten(
            "", hermes_home=tmp_path / ".hermes"
        ) is None
