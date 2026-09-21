"""Full backup failures remain auditable beyond the console preview."""

import logging
import zipfile
from argparse import Namespace

from hermes_cli import backup


def test_full_backup_logs_every_missing_entry_and_preserves_last_good(
    tmp_path, monkeypatch, caplog, capsys,
):
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("model: test\n", encoding="utf-8")
    missing = [home / f"rotated-{i}.md" for i in range(12)]
    for path in missing:
        path.write_text("transcript\n", encoding="utf-8")
    entries = [(path, path.relative_to(home)) for path in [config, *missing]]

    def walk(*args):
        yield from entries
        for path in missing:
            path.unlink()

    monkeypatch.setattr(backup, "_iter_backup_files", walk)
    monkeypatch.setattr(backup, "_collect_external_entries", lambda: ([], []))
    previous = tmp_path / "hermes-backup-previous.zip"
    previous.write_bytes(b"preserved recovery artifact")
    output = tmp_path / "hermes-backup-current.zip"
    with caplog.at_level(logging.WARNING, logger=backup.__name__):
        result = backup._run_backup_locked(Namespace(output=str(output), keep=1), home)

    assert result is False
    assert previous.read_bytes() == b"preserved recovery artifact"
    with zipfile.ZipFile(output) as archive:
        assert archive.read("config.yaml") == config.read_bytes()
    diagnostics = [r.getMessage() for r in caplog.records if "entry_failure=" in r.getMessage()]
    assert len(diagnostics) == len(missing)
    for path in missing:
        assert any(path.name in message for message in diagnostics)
    assert "... and 2 more" in capsys.readouterr().out
