"""CLI exports read real temporary stores without acquiring writer privileges."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_state import SessionDB


# Audit real SQLite opens without substituting the CLI, dispatch, or store.
_CLI = """
import sys

def record_open(event, args):
    if event == "sqlite3.connect":
        print("SESSION_DB_OPEN " + str(args[0]), file=sys.stderr)

sys.addaudithook(record_open)
from hermes_cli.main import main
main()
"""


def _run_cli(tmp_path, home, *args, profile=None):
    env = dict(os.environ, HOME=str(tmp_path / "user"), HERMES_HOME=str(home),
               HERMES_SESSION_PROFILE="other")
    prefix = ["-p", profile] if profile else []
    result = subprocess.run(
        [sys.executable, "-c", _CLI, *prefix, "sessions", "export", *args],
        env=env, cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=60, check=False,
    )
    return result


def _assert_reader(result):
    opens = [line.removeprefix("SESSION_DB_OPEN ") for line in result.stderr.splitlines()
             if line.startswith("SESSION_DB_OPEN ") and "state.db" in line]
    assert opens, result.stderr
    assert all("mode=ro" in path for path in opens), opens


def _seed(home):
    home.mkdir(parents=True, exist_ok=True)
    path = home / "state.db"
    db = SessionDB(db_path=path)
    try:
        db.create_session("export-target", "cli")
        db.append_message("export-target", "user", "reader prompt")
        db.append_message("export-target", "assistant", "reader answer")
    finally:
        db.close()
    return path


def _snapshot(path):
    # All SessionDB handles are closed before the byte read, including WAL lock guards.
    before = path.read_bytes()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        schema = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    finally:
        conn.close()
    return before, schema


@pytest.mark.parametrize("fmt,extra", [
    ("jsonl", []), ("html", []), ("md", []), ("qmd", []), ("trace", []),
    ("jsonl", ["--only", "user-prompts"]), ("md", ["--lineage", "logical"]),
])
def test_export_uses_reader_and_preserves_store(tmp_path, fmt, extra):
    home = tmp_path / "profiles" / "work"
    path = _seed(home)
    before = _snapshot(path)
    output = "-" if fmt in ("jsonl", "trace") else str(tmp_path / "export")

    result = _run_cli(tmp_path, home, "--session-id", "export-target", "--format", fmt, *extra, output)

    assert result.returncode == 0, result.stdout + result.stderr
    _assert_reader(result)
    if output == "-":
        assert "reader prompt" in result.stdout
        if fmt == "jsonl" and not extra:
            assert json.loads(result.stdout)["id"] == "export-target"
    elif fmt == "html":
        assert "reader prompt" in Path(output).read_text(encoding="utf-8")
    else:
        assert "reader prompt" in next(Path(output).glob(f"*.{fmt}")).read_text(encoding="utf-8")
    assert _snapshot(path) == before


@pytest.mark.parametrize("explicit_profile", [False, True])
def test_exact_jsonl_export_succeeds_on_read_only_selected_store(tmp_path, explicit_profile):
    root = tmp_path / ".hermes"
    home = root / "profiles" / "work"
    path = _seed(home)
    other = _seed(root / "profiles" / "other")
    path.chmod(0o444)
    before, other_before = _snapshot(path), _snapshot(other)
    try:
        result = _run_cli(
            tmp_path, root if explicit_profile else home,
            "--session-id", "export-target", "--format", "jsonl", "-",
            profile="work" if explicit_profile else None,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        _assert_reader(result)
        assert json.loads(result.stdout)["id"] == "export-target"
        assert str(home) in result.stderr
        assert str(other.parent) not in result.stderr
        assert _snapshot(path) == before
        assert _snapshot(other) == other_before
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize("selection", [[], ["--session-id", "missing"]])
def test_export_missing_store_does_not_create_it(tmp_path, selection):
    home = tmp_path / "empty"
    home.mkdir()
    output = tmp_path / "absent.jsonl"
    result = _run_cli(tmp_path, home, "--format", "jsonl", *selection, str(output))
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_reader(result)
    assert "No sessions found." in result.stdout
    assert not list(home.glob("state.db*"))
    assert not output.exists()


def test_export_legacy_store_reports_migration_without_changing_it(tmp_path):
    home = tmp_path / "legacy"
    home.mkdir()
    path = home / "state.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO sessions VALUES ('export-target')")
        conn.commit()
    finally:
        conn.close()
    before = _snapshot(path)
    result = _run_cli(tmp_path, home, "--session-id", "export-target", "--format", "jsonl", "-")
    assert result.returncode != 0, result.stdout + result.stderr
    _assert_reader(result)
    assert "needs migration" in result.stdout
    assert _snapshot(path) == before
    assert not list(home.glob("state.db-*"))


@pytest.mark.parametrize("fmt", ["md", "qmd"])
def test_verified_delete_keeps_writer_and_requires_confirmation(tmp_path, fmt):
    home = tmp_path / "home"
    path = _seed(home)
    before = _snapshot(path)
    output = tmp_path / "export"
    args = ["--session-id", "export-target", "--format", fmt, "--delete-after-verified", str(output)]

    refused = _run_cli(tmp_path, home, *args)
    assert refused.returncode == 1, refused.stdout + refused.stderr
    _assert_reader(refused)
    assert "requires --yes" in refused.stdout
    assert not output.exists()
    assert _snapshot(path) == before

    deleted = _run_cli(tmp_path, home, *args, "--yes")
    assert deleted.returncode == 0, deleted.stdout + deleted.stderr
    assert "Deleted exported session 'export-target'." in deleted.stdout
    assert f"SESSION_DB_OPEN {path}" in deleted.stderr
    assert "reader answer" in next(output.glob(f"*.{fmt}")).read_text(encoding="utf-8")
    db = SessionDB(db_path=path, read_only=True)
    try:
        assert db.get_session("export-target") is None
    finally:
        db.close()
