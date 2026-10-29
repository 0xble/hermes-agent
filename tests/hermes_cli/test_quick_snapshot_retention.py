"""Recovery retention regressions for upstream #106087, adapted from #106101."""

import json
import logging
import os
import sqlite3
from collections import Counter

import pytest

from hermes_cli import backup


def _home(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: {}\n")
    for rel in ("state.db", "cron/executions.db"):
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE records (value TEXT)")
            conn.execute("INSERT INTO records VALUES (?)", ("r" * 10000 if rel == "state.db" else "recovery",))
    return home


def test_repeated_failed_captures_are_bounded_without_losing_complete_recovery(tmp_path, monkeypatch):
    home = _home(tmp_path)
    complete = backup.create_quick_snapshot(hermes_home=home, label="pre-update", keep=1)
    assert complete
    real_copy = backup._safe_copy_db
    monkeypatch.setattr(backup, "_safe_copy_db", lambda src, dst: False if src.name == "state.db" else real_copy(src, dst))
    partials = [backup.create_quick_snapshot(hermes_home=home, label="pre-update", keep=1) for _ in range(4)]
    assert all(partials)
    root = home / "state-snapshots"
    assert {p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")} == {complete, partials[-1]}
    assert backup.verify_sqlite_integrity(root / complete / "state.db")["valid"]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("failed_rel", ["config.yaml", ".env", "auth.json", "cron/jobs.json"])
def test_unreadable_file_retains_its_last_recovery_copy(
        tmp_path, monkeypatch, capsys, caplog, failed_rel):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    failed = home / failed_rel
    failed.parent.mkdir(parents=True, exist_ok=True)
    original = b"critical recovery state\n"
    failed.write_bytes(original)
    (home / "gateway_state.json").write_text("{}\n")
    # Neither generation is complete: a complete-anchor guard must not accidentally
    # mask failure to record the unreadable non-DB path as an omission.
    (home / "state.db").write_bytes(b"x" * 2048)
    previous = backup.create_quick_snapshot(
        hermes_home=home, label="pre-update", keep=1, max_file_size=1024)
    assert previous
    root = home / "state-snapshots"
    anchor = root / previous
    assert (anchor / failed_rel).read_bytes() == original
    failed.chmod(0)
    try:
        if os.access(failed, os.R_OK):
            pytest.skip("chmod 0 does not make the file unreadable for this user")
        caplog.set_level(logging.WARNING, logger=backup.__name__)
        for _ in range(3):
            latest = backup.create_quick_snapshot(
                hermes_home=home, label="pre-update", keep=1, max_file_size=1024)
            assert latest
            assert anchor.exists(), "copy failure pruned the last recovery generation"
            assert (anchor / failed_rel).read_bytes() == original
            current = root / latest
            meta = json.loads((current / "manifest.json").read_text())
            assert meta["failed_files"] == [failed_rel]
            assert failed_rel not in meta["files"]
            assert (current / "gateway_state.json").read_text() == "{}\n"
            assert {p for p in root.iterdir() if p.is_dir()} == {anchor, current}
            assert f"could not snapshot {failed_rel}" in capsys.readouterr().out
            assert f"Could not snapshot {failed_rel}" in caplog.text
        # An independent prune must recover the omission from disk, not run-local state.
        backup.prune_quick_snapshots(keep=1, hermes_home=home)
        assert (anchor / failed_rel).read_bytes() == original
    finally:
        failed.chmod(0o600)
    healed = backup.create_quick_snapshot(
        hermes_home=home, label="pre-update", keep=1, max_file_size=1024)
    assert (root / healed / failed_rel).read_bytes() == original
    assert not anchor.exists(), "a successful capture should release the older anchor"


def test_manual_and_pre_update_snapshots_have_independent_retention(tmp_path):
    home = _home(tmp_path)
    manual = backup.create_quick_snapshot(hermes_home=home, label="manual", keep=1)
    automatic = backup.create_quick_snapshot(hermes_home=home, label="pre-update", keep=1)
    latest = backup.create_quick_snapshot(hermes_home=home, label="pre-update", keep=1)
    root = home / "state-snapshots"
    assert (root / manual).exists()
    assert (root / latest).exists()
    assert not (root / automatic).exists()


def test_unusable_claimed_copy_cannot_replace_complete_generation(tmp_path, monkeypatch):
    home = _home(tmp_path)
    complete = backup.create_quick_snapshot(hermes_home=home, label="pre-update", keep=1)
    assert complete
    real_copy = backup._safe_copy_db
    monkeypatch.setattr(backup, "_safe_copy_db", lambda src, dst: False if src.name == "executions.db" else real_copy(src, dst))
    newer = backup.create_quick_snapshot(hermes_home=home, label="pre-update", keep=1)
    root = home / "state-snapshots"
    claimed = root / newer / "state.db"
    claimed.unlink()
    with sqlite3.connect(claimed) as conn:
        conn.execute("CREATE TABLE records (value TEXT)")
        conn.execute("INSERT INTO records VALUES ('replacement')")
    assert backup.verify_sqlite_integrity(claimed)["valid"]
    assert claimed.stat().st_size != (root / complete / "state.db").stat().st_size
    monkeypatch.setattr(backup, "_safe_copy_db", lambda src, dst: False if src.name == "state.db" else real_copy(src, dst))
    latest = backup.create_quick_snapshot(hermes_home=home, label="pre-update", keep=1)
    assert latest
    assert (root / complete / "state.db").exists(), "unusable payload displaced the last complete generation"
    assert backup.verify_sqlite_integrity(root / complete / "state.db")["valid"]
    assert (root / latest).exists()
    assert not (root / newer).exists(), "stale partial generations must not accumulate"


@pytest.mark.parametrize("keep", [1, 4])
def test_pruning_only_verifies_live_recovery_anchors_once(tmp_path, monkeypatch, keep):
    home = _home(tmp_path)
    snapshots = [backup.create_quick_snapshot(hermes_home=home, keep=20) for _ in range(5)]
    root = home / "state-snapshots"
    hashes, checks = Counter(), Counter()
    from hermes_cli import backup_snapshot_integrity as integrity
    digest, verify = integrity.payload_digest, backup.verify_sqlite_integrity
    def traced_digest(path):
        hashes[path] += 1
        return digest(path)
    def traced_verify(path):
        checks[path] += 1
        return verify(path)
    monkeypatch.setattr(integrity, "payload_digest", traced_digest)
    monkeypatch.setattr(backup, "verify_sqlite_integrity", traced_verify)
    backup.prune_quick_snapshots(keep=keep, hermes_home=home)
    latest = root / snapshots[-1]
    assert {p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")} == {
        root / name for name in snapshots[-keep:]}
    assert hashes and all(path.is_relative_to(latest) and count == 1 for path, count in hashes.items())
    assert checks and all(path.is_relative_to(latest) and count == 1 for path, count in checks.items())


def test_pruning_caches_failed_and_successful_recovery_checks(tmp_path, monkeypatch):
    home = _home(tmp_path)
    snapshots = [backup.create_quick_snapshot(hermes_home=home, keep=20) for _ in range(3)]
    root = home / "state-snapshots"
    latest = root / snapshots[-1]
    changed = latest / "state.db"
    size = changed.stat().st_size
    with sqlite3.connect(changed) as conn:
        conn.execute("UPDATE records SET value=?", ("x" * 10000,))
    assert changed.stat().st_size == size and backup.verify_sqlite_integrity(changed)["valid"]
    from hermes_cli import backup_snapshot_integrity as integrity
    hashes, checks = Counter(), Counter()
    digest, verify = integrity.payload_digest, backup.verify_sqlite_integrity
    def traced_digest(path):
        hashes[path] += 1
        return digest(path)
    def traced_verify(path):
        checks[path] += 1
        return verify(path)
    monkeypatch.setattr(integrity, "payload_digest", traced_digest)
    monkeypatch.setattr(backup, "verify_sqlite_integrity", traced_verify)
    backup.prune_quick_snapshots(keep=1, hermes_home=home)
    genuine = root / snapshots[-2]
    assert latest.exists() and genuine.exists() and not (root / snapshots[0]).exists()
    assert hashes and all(count == 1 for count in hashes.values())
    assert checks and all(count == 1 for count in checks.values())
    assert not any(path.is_relative_to(root / snapshots[0]) for path in hashes)
    assert changed not in checks, "a digest mismatch must fail before SQLite verification"
