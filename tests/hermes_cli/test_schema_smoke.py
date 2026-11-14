"""The update's schema smoke: a candidate whose migration never settles on the installed
profile's database shape is refused before activation."""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import schema_smoke

_FORK_LEDGER = """CREATE TABLE executions (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
    process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started_at INTEGER,
    status TEXT NOT NULL CHECK(status IN
      ('claimed','running','completed','failed','unknown','deferred')),
    claimed_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT)"""


def _installed_profile(tmp_path: Path) -> Path:
    """A profile whose execution ledger has the fork shape upstream's migration never matched."""
    home = tmp_path / "installed"
    (home / "cron").mkdir(parents=True)
    with sqlite3.connect(home / "cron" / "executions.db") as conn:
        conn.execute(_FORK_LEDGER)
        conn.execute("INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at)"
                     " VALUES ('user-row', 'j1', 'builtin', 'p', 1, 'deferred', '2026-11-01T00:00:00+00:00')")
    return home


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    home = tmp_path / "scratch"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_the_current_ledger_migration_settles_on_a_fork_shaped_profile(tmp_path, scratch):
    assert schema_smoke.check(_installed_profile(tmp_path), scratch) == ([], [])
    with sqlite3.connect(scratch / "cron" / "executions.db") as conn:
        assert conn.execute("SELECT count(*) FROM executions").fetchone() == (0,)  # schema only, no rows


def test_the_v0231_ledger_migration_is_refused_on_a_fork_shaped_profile(tmp_path, scratch, monkeypatch):
    """Upstream v0.23.1 widened the status CHECK by replacing its own stored text. On the fork's
    ledger the replace matched nothing, so the table was rebuilt on every open."""
    from cron import executions

    monkeypatch.setattr(executions, "_widened_table_sql",
                        lambda old: old.replace("'unknown')", "'unknown','cancelled','missed')"))

    failures, skipped = schema_smoke.check(_installed_profile(tmp_path), scratch)

    assert skipped == []
    assert len(failures) == 1 and failures[0].startswith(
        "cron/executions.db schema is still changing on the second open (")
    assert "table executions" in failures[0]


def test_a_probe_that_cannot_run_is_reported_and_never_blocks(tmp_path, scratch, monkeypatch):
    def broken():
        raise RuntimeError("initializer needs rows the copy does not carry")
    monkeypatch.setattr(schema_smoke, "DATABASES", {"cron/executions.db": broken})

    assert schema_smoke.check(_installed_profile(tmp_path), scratch) == (
        [], ["cron/executions.db: RuntimeError: initializer needs rows the copy does not carry"])


def test_the_copy_carries_virtual_tables_and_the_version_row_but_no_data(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "copy" / "target.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version VALUES (33)")
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")
        conn.execute("INSERT INTO messages (content) VALUES ('private')")
        conn.execute("CREATE INDEX idx_messages_content ON messages(content)")
        conn.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content)")

    schema_smoke.copy_schema(source, target)

    with sqlite3.connect(target) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert {"messages", "idx_messages_content", "messages_fts", "messages_fts_data"} <= names
        assert conn.execute("SELECT version FROM schema_version").fetchall() == [(33,)]
        assert conn.execute("SELECT count(*) FROM messages").fetchone() == (0,)


def test_the_module_entry_point_exits_nonzero_and_names_the_database(tmp_path):
    """The staging step runs this module in the candidate's interpreter; drive it the same way."""
    home, scratch_home = _installed_profile(tmp_path), tmp_path / "scratch"
    scratch_home.mkdir()
    root = Path(__file__).resolve().parents[2]
    bootstrap = (
        "import sys, cron.executions as e\n"
        "e._widened_table_sql = lambda old: old.replace(\"'unknown')\", \"'unknown','cancelled','missed')\")\n"
        "from hermes_cli import schema_smoke\n"
        "raise SystemExit(schema_smoke.main(sys.argv))\n")
    env = {"HERMES_HOME": str(scratch_home), "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
    broken = subprocess.run([sys.executable, "-c", bootstrap, "--source-home", str(home)],
                            cwd=root, env=env, capture_output=True, text=True, check=False)
    assert broken.returncode == 3 and "cron/executions.db schema is still changing" in broken.stderr

    clean = subprocess.run([sys.executable, "-m", "hermes_cli.schema_smoke", "--source-home", str(home)],
                           cwd=root, env=env, capture_output=True, text=True, check=False)
    assert (clean.returncode, clean.stderr) == (0, "")


def test_staging_refuses_a_candidate_whose_schema_smoke_fails(tmp_path, monkeypatch):
    """The staging step turns the probe's verdict into a refused release, and skips a rollback
    target that predates the probe."""
    from hermes_cli import immutable_releases as releases

    release = tmp_path / "release"
    (release / "hermes_cli").mkdir(parents=True)
    monkeypatch.setattr("pm.environments.project_python", lambda _release: Path(sys.executable))
    ran = []
    exit_code = 3

    def run(argv, **kwargs):
        ran.append(argv[1:])
        return subprocess.CompletedProcess(argv, exit_code, "", "schema smoke skipped state.db: probe\n"
                                           "cron/executions.db schema is still changing on the second open (x)\n")
    monkeypatch.setattr(releases.subprocess, "run", run)

    releases.smoke_schemas(release, tmp_path / "profile")  # no probe in this release: nothing runs
    assert ran == []

    (release / "hermes_cli" / "schema_smoke.py").write_text("")
    exit_code = 1  # the probe crashed: reported, never a refusal
    releases.smoke_schemas(release, tmp_path / "profile")
    ran.clear()
    exit_code = 3
    with pytest.raises(RuntimeError, match=r"candidate schema smoke failed: cron/executions\.db schema is still"):
        releases.smoke_schemas(release, tmp_path / "profile")
    assert ran == [["-m", "hermes_cli.schema_smoke", "--source-home", str(tmp_path / "profile")]]
