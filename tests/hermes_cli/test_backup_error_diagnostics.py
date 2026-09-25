"""Full backup failures remain auditable beyond the console preview."""

import logging
import zipfile
from argparse import Namespace

from hermes_cli import backup


def test_full_backup_logs_every_unreadable_entry_and_preserves_last_good(
    tmp_path, monkeypatch, caplog, capsys,
):
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("model: test\n", encoding="utf-8")
    # These exist and cannot be READ. A file that is merely gone takes the vanished
    # path instead, which is a different outcome and is covered separately below.
    unreadable_files = [home / f"rotated-{i}.md" for i in range(12)]
    for path in unreadable_files:
        path.write_text("transcript\n", encoding="utf-8")
    entries = [(path, path.relative_to(home)) for path in [config, *unreadable_files]]

    def walk(*args):
        yield from entries

    write = zipfile.ZipFile.write

    def denied_write(archive, filename, *args, **kwargs):
        if filename in unreadable_files:
            raise PermissionError(f"cannot read {filename}")
        return write(archive, filename, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "write", denied_write)

    monkeypatch.setattr(backup, "_iter_backup_files", walk)
    monkeypatch.setattr(backup, "_collect_external_entries", lambda: ([], []))
    # _newest_first orders by NAME, and "previous" sorts above "current", so a regressed
    # prune gate would delete the NEW archive with this assertion still passing. Timestamped
    # names make the ordering mean what the test says it means. Confirmed by mutation probe:
    # dropping `not errors` from the gate at backup.py fails here, and did not before.
    previous = tmp_path / "hermes-backup-2026-09-19-120000.zip"
    previous.write_bytes(b"preserved recovery artifact")
    output = tmp_path / "hermes-backup-2026-09-20-120000.zip"
    with caplog.at_level(logging.WARNING, logger=backup.__name__):
        result = backup._run_backup_locked(Namespace(output=str(output), keep=1), home)

    assert result is False
    assert previous.read_bytes() == b"preserved recovery artifact"
    with zipfile.ZipFile(output) as archive:
        assert archive.read("config.yaml") == config.read_bytes()
    diagnostics = [r.getMessage() for r in caplog.records if "entry_failure=" in r.getMessage()]
    assert len(diagnostics) == len(unreadable_files)
    for path in unreadable_files:
        assert any(path.name in message for message in diagnostics)
    assert "... and 2 more" in capsys.readouterr().out


def test_vanished_entries_reach_the_profile_log(tmp_path, monkeypatch, caplog):
    """The vanished entries are recorded in the log, not just the console.

    #38 already covers the decision itself: four tests at test_backup.py:772-932 pin the
    return value, the critical-file exception, the nested-basename case and the
    mass-vanish prune ceiling. None of them look at the log, and `entry_vanished=` is
    asserted nowhere in the suite. That is the same gap #36 closed for failures: the
    console preview is capped, so an operator diagnosing a run days later has only the
    profile log to work from.
    """
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("model: test\n", encoding="utf-8")
    disappearing = [home / f"rotated-{i}.md" for i in range(12)]
    for path in disappearing:
        path.write_text("transcript\n", encoding="utf-8")
    entries = [(path, path.relative_to(home)) for path in [config, *disappearing]]

    def walk(*args):
        yield from entries
        for path in disappearing:
            path.unlink()

    monkeypatch.setattr(backup, "_iter_backup_files", walk)
    monkeypatch.setattr(backup, "_collect_external_entries", lambda: ([], []))
    output = tmp_path / "hermes-backup-2026-09-20-120000.zip"
    with caplog.at_level(logging.INFO, logger=backup.__name__):
        result = backup._run_backup_locked(Namespace(output=str(output), keep=1), home)

    assert result is True
    messages = [record.getMessage() for record in caplog.records]
    assert not [m for m in messages if "entry_failure=" in m], messages
    vanished = [m for m in messages if "entry_vanished=" in m]
    assert len(vanished) == len(disappearing)
    for path in disappearing:
        assert any(path.name in message for message in vanished)
    with zipfile.ZipFile(output) as archive:
        assert archive.read("config.yaml") == config.read_bytes()
