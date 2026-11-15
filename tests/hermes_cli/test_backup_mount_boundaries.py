"""Filesystem-boundary tests for Hermes backup walkers."""

import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace


def test_full_backup_skips_foreign_device_but_keeps_same_device(tmp_path, monkeypatch, caplog):
    import hermes_cli.backup as backup_mod

    root = tmp_path / ".hermes"
    root.mkdir()
    same = root / "same-device"
    foreign = root / "foreign-device"
    same.mkdir()
    foreign.mkdir()
    (same / "kept.txt").write_text("keep")
    (foreign / "not-walked.txt").write_text("do not read")
    real_lstat = backup_mod.os.lstat

    def foreign_device_lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        if Path(path) == foreign:
            return SimpleNamespace(st_dev=info.st_dev + 1, st_mode=info.st_mode)
        return info

    monkeypatch.setattr(backup_mod.os, "lstat", foreign_device_lstat)
    skipped: set[str] = set()
    with caplog.at_level(logging.INFO, logger=backup_mod.logger.name):
        selected = {
            str(rel)
            for _, rel in backup_mod._iter_backup_files(root, tmp_path / "out.zip", skipped)
        }

    assert "same-device/kept.txt" in selected
    assert "foreign-device/not-walked.txt" not in selected
    assert skipped == {"foreign-device"}
    assert [
        r.getMessage() for r in caplog.records
        if "skipped mount point=foreign-device" in r.getMessage()
    ] == ["backup skipped mount point=foreign-device"]


def test_quick_snapshot_manifest_records_mount_skip(tmp_path, monkeypatch):
    import hermes_cli.backup as backup_mod

    home = tmp_path / ".hermes"
    home.mkdir()
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE sample (value TEXT)")
    boards = home / "kanban" / "boards"
    boards.mkdir(parents=True)
    same = boards / "same"
    foreign = boards / "foreign"
    same.mkdir()
    foreign.mkdir()
    (same / "kept.txt").write_text("keep")
    (foreign / "not-walked.txt").write_text("do not read")
    real_ismount = backup_mod.os.path.ismount
    monkeypatch.setattr(
        backup_mod.os.path,
        "ismount",
        lambda path: Path(path) == foreign or real_ismount(path),
    )

    snapshot_id = backup_mod.create_quick_snapshot(hermes_home=home, keep=1)
    assert snapshot_id
    manifest = json.loads(
        (home / "state-snapshots" / snapshot_id / "manifest.json").read_text()
    )
    assert "kanban/boards/same/kept.txt" in manifest["files"]
    assert "kanban/boards/foreign/not-walked.txt" not in manifest["files"]
    assert manifest["skipped_mounts"] == ["kanban/boards/foreign"]
