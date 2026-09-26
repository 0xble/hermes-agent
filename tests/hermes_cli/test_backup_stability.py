from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from hermes_cli.backup import (
    BackupInProgressError,
    _atomic_output_path,
    _backup_operation_lock,
    _write_full_zip_backup,
    create_quick_snapshot,
    list_quick_snapshots,
)


def test_backup_lock_rejects_a_second_operation(tmp_path) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()

    with _backup_operation_lock(home):
        with pytest.raises(BackupInProgressError):
            with _backup_operation_lock(home, timeout_seconds=0):
                raise AssertionError("second backup unexpectedly acquired the lock")


def test_atomic_output_publishes_only_after_clean_close(tmp_path) -> None:
    final = tmp_path / "backup.zip"
    final.write_bytes(b"previous")

    with _atomic_output_path(final) as partial:
        partial.write_bytes(b"complete")
        assert final.read_bytes() == b"previous"

    assert final.read_bytes() == b"complete"
    assert not partial.exists()


def test_atomic_output_keeps_previous_file_after_failure(tmp_path) -> None:
    final = tmp_path / "backup.zip"
    final.write_bytes(b"previous")

    with pytest.raises(RuntimeError):
        with _atomic_output_path(final) as partial:
            partial.write_bytes(b"incomplete")
            raise RuntimeError("compression failed")

    assert final.read_bytes() == b"previous"
    assert not partial.exists()


def test_quick_snapshot_is_published_with_manifest(tmp_path, monkeypatch) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    published: list[tuple[Path, Path]] = []

    from hermes_cli import backup

    real_replace = backup.os.replace

    def replace(source, destination) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path.parent == home / "state-snapshots":
            assert source_path.name.endswith(".partial")
            assert (source_path / "manifest.json").is_file()
            assert not destination_path.exists()
            published.append((source_path, destination_path))
        real_replace(source, destination)

    monkeypatch.setattr(backup.os, "replace", replace)
    snapshot_id = create_quick_snapshot(hermes_home=home)

    assert snapshot_id is not None
    assert len(published) == 1
    manifest = json.loads(
        (home / "state-snapshots" / snapshot_id / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["id"] == snapshot_id
    assert manifest["files"] == {"config.yaml": 10}


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_quick_snapshot_tree_is_owner_only_under_permissive_umask(tmp_path) -> None:
    """Recovery snapshots must never inherit world-readable default modes.

    A normal 0022 umask creates SQLite databases and JSON files as 0644 and
    directories as 0755.  Quick snapshots contain session state, credentials,
    pairing records, and cron data, so every published file must be 0600 and
    every directory 0700 regardless of the caller's umask or source modes.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")

    old_umask = os.umask(0o022)
    try:
        snapshot_id = create_quick_snapshot(hermes_home=home)
    finally:
        os.umask(old_umask)

    assert snapshot_id is not None
    root = home / "state-snapshots"
    snapshot = root / snapshot_id
    directories = [root, snapshot, *(p for p in snapshot.rglob("*") if p.is_dir())]
    files = [p for p in snapshot.rglob("*") if p.is_file()]

    assert directories
    assert files
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in directories)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)


def test_quick_snapshot_listing_ignores_partial_directories(tmp_path) -> None:
    home = tmp_path / ".hermes"
    partial = home / "state-snapshots" / ".unfinished.1.partial"
    partial.mkdir(parents=True)
    (partial / "manifest.json").write_text('{"id":"unfinished"}', encoding="utf-8")

    assert list_quick_snapshots(hermes_home=home) == []


def test_failed_automatic_backup_preserves_previous_archive(tmp_path, monkeypatch) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "state.db").write_bytes(b"not-a-database")
    archive = tmp_path / "automatic.zip"
    archive.write_bytes(b"previous-valid-backup")

    monkeypatch.setattr("hermes_cli.backup._safe_copy_db", lambda _src, _dst: False)

    assert _write_full_zip_backup(archive, home) is None
    assert archive.read_bytes() == b"previous-valid-backup"
    assert list(tmp_path.glob(".*.partial")) == []


#: Cap below the database so it is skipped for SIZE — a standing property of the file,
#: not a transient failure. This is what a real install looks like: a 32 GB state.db
#: against a 1 GiB cap, on every run, forever.
_TINY_CAP = 1024


def _home_with_oversized_db(tmp_path):
    """A home whose state.db is past the snapshot size cap, as a real install's is."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    (home / "state.db").write_bytes(b"x" * 4096)
    return home


def test_a_permanently_oversized_db_does_not_disable_pruning_forever(tmp_path) -> None:
    """Skipping a file for SIZE must not latch the prune off.

    The guard exists so an incomplete snapshot never deletes the last good one. Size is
    not a transient failure though: the condition never clears, so a state.db past the cap
    made the branch permanent and snapshots grew without bound. Observed on a live install
    as 26 directories and 32 GB, every one of them also missing that database — so the
    guard was preserving snapshots that did not contain the thing it was protecting.
    """
    home = _home_with_oversized_db(tmp_path)
    root = home / "state-snapshots"

    ids = [create_quick_snapshot(hermes_home=home, keep=1, max_file_size=_TINY_CAP) for _ in range(3)]
    assert all(ids), "each snapshot must still publish"

    remaining = sorted(d.name for d in root.iterdir() if d.is_dir())
    assert len(remaining) == 1, f"keep=1 must actually prune, got {remaining}"
    assert remaining == [ids[-1]]


def test_a_snapshot_still_holding_the_oversized_db_is_never_pruned(tmp_path) -> None:
    """Pruning may not drop the only copy of a database later runs had to skip."""
    home = _home_with_oversized_db(tmp_path)
    root = home / "state-snapshots"
    # Capture a real, manifest-listed SQLite database before it exceeds the cap.
    (home / "state.db").unlink()
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE evidence (value TEXT)")
        conn.execute("INSERT INTO evidence VALUES ('recovery')")
    first = create_quick_snapshot(hermes_home=home, keep=1)
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("INSERT INTO evidence VALUES (?)", ("x" * 4096,))

    for _ in range(3):
        create_quick_snapshot(hermes_home=home, keep=1, max_file_size=_TINY_CAP)

    assert (root / first / "state.db").exists(), "the only copy must survive the prune"


def test_an_oversized_non_database_is_protected_too(tmp_path) -> None:
    """Protection is about what this run omitted, not about file type.

    Gating it on ``.db`` meant an oversized auth.json got none, so the prune could drop
    the only copy of it while happily protecting a database beside it.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    (home / "auth.json").write_bytes(b"the only copy")
    root = home / "state-snapshots"

    first = create_quick_snapshot(hermes_home=home, keep=1)
    (home / "auth.json").write_bytes(b"y" * 4096)

    for _ in range(3):
        create_quick_snapshot(hermes_home=home, keep=1, max_file_size=_TINY_CAP)

    assert (root / first / "auth.json").exists(), "the only copy must survive the prune"


def test_abandoned_staging_is_reclaimed(tmp_path) -> None:
    """Only the creating process removes staging, and the prune deliberately skips
    ``.partial``, so a killed run leaked one permanently. Observed as 22 GB abandoned."""
    home = _home_with_oversized_db(tmp_path)
    root = home / "state-snapshots"
    root.mkdir(parents=True, exist_ok=True)
    orphan = root / ".20260101-000000.99999.partial"
    orphan.mkdir()
    (orphan / "state.db").write_bytes(b"half copied")

    assert create_quick_snapshot(hermes_home=home, keep=1, max_file_size=_TINY_CAP) is not None
    assert not orphan.exists(), "abandoned staging must be reclaimed"
