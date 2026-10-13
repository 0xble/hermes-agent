"""Behavior checks for the disposable-copy rehearsal gate."""
import importlib.util
import json
import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture
def rehearsal():
    source = Path(__file__).resolve().parents[2] / "scripts/schema_rehearsal.py"
    spec = importlib.util.spec_from_file_location("schema_rehearsal", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def v2_copy(tmp_path):
    from hermes_state import SessionDB
    from hermes_state_common import FTS_TOOL_CONTENT_PREFIX_CHARS
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.close()
    with sqlite3.connect(path) as c:
        from gateway.delivery_ledger import _initialize_schema
        _initialize_schema(c)
        c.execute("INSERT INTO sessions(id, source, started_at) VALUES('fixture', 'cli', ?)", (time.time(),))
        c.executemany("INSERT INTO messages(session_id, role, content, timestamp) VALUES('fixture', ?, ?, ?)", [
            ('tool', 'telegram ' + 'x' * FTS_TOOL_CONTENT_PREFIX_CHARS + ' backup', time.time()),
            ('user', 'hermes backup', time.time()),
        ])
        for name in ('messages_fts_insert', 'messages_fts_delete', 'messages_fts_update'):
            c.execute(f'DROP TRIGGER IF EXISTS {name}')
        c.execute('DROP TABLE messages_fts')
        c.execute('DROP VIEW messages_fts_src')
        c.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content, tool_name, tool_calls, content='messages', content_rowid='id')")
        c.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        c.executemany('INSERT OR REPLACE INTO state_meta(key,value) VALUES(?,?)', [
            ('fts_storage_version', '2'), ('fts_tool_full_content_high_water', '2'), ('fixture_important', 'retain'),
        ])
        c.execute('CREATE TABLE fork_ledger(value TEXT)')
        c.execute("INSERT INTO fork_ledger VALUES('pending obligation')")
    return path


@pytest.mark.parametrize('case', ['valid', 'corrupt_index', 'wrong_projection', 'lost_message',
                                  'lost_legacy_row', 'lost_metadata', 'probe_error', 'open_exit', 'integrity_error'])
def test_rehearsal_real_transition_is_narrow(rehearsal, v2_copy, case):
    from hermes_state import SessionDB
    before, pre = rehearsal.snapshot(v2_copy), rehearsal.probes(v2_copy)
    with sqlite3.connect(v2_copy) as c:
        canonical_before = c.execute('SELECT id, session_id, role, content FROM messages ORDER BY id').fetchall()
    db = SessionDB(db_path=v2_copy)
    db.close()
    with sqlite3.connect(v2_copy) as c:
        assert c.execute('SELECT id, session_id, role, content FROM messages ORDER BY id').fetchall() == canonical_before
        if case == 'corrupt_index':
            c.execute("INSERT INTO messages_fts(messages_fts) VALUES('delete-all')")
        elif case == 'wrong_projection':
            c.execute('DROP VIEW messages_fts_src')
            c.execute('CREATE VIEW messages_fts_src AS SELECT id, substr(content,1,1) AS content, tool_name, tool_calls FROM messages')
            c.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        elif case == 'lost_message':
            c.execute('DELETE FROM messages WHERE id=(SELECT MAX(id) FROM messages)')
        elif case == 'lost_legacy_row':
            c.execute('DELETE FROM fork_ledger')
        elif case == 'lost_metadata':
            c.execute("DELETE FROM state_meta WHERE key='fixture_important'")
    after, post = rehearsal.snapshot(v2_copy), rehearsal.probes(v2_copy)
    opened = {'opened': True, 'exit': 1 if case == 'open_exit' else 0}
    integrity = {'result': [('corrupt',)]} if case == 'integrity_error' else None
    if case == 'probe_error':
        pre['fts:hermes'] = post['fts:hermes'] = 'err identical failure'
    result = rehearsal.assess(v2_copy, before, after, pre, post, opened, integrity)
    assert result['ok'] is (case == 'valid'), json.dumps(result, indent=2)
    if case == 'valid':
        assert pre['fts:backup'] == [(2,)] and post['fts:backup'] == [(1,)]
        assert pre['fts:telegram'] == post['fts:telegram'] == [(1,)]
        assert result['fts_transition_validation']['integrity'] == 'rank=1'
        assert result['expected_derived_changes']['metadata_removed'] == ['fts_tool_full_content_high_water']


@pytest.mark.parametrize('in_worktree', [False, True])
def test_declared_tables_filters_relative_directories(rehearsal, tmp_path, monkeypatch, in_worktree):
    root = tmp_path / '.worktrees' / 'candidate' if in_worktree else tmp_path / 'candidate'
    root.mkdir(parents=True)
    (root / 'core.py').write_text('SQL = "CREATE TABLE canonical_records(id TEXT)"')
    for excluded in ('tests', '.venv', 'venv', '.worktrees'):
        directory = root / excluded
        directory.mkdir()
        (directory / 'hidden.py').write_text('SQL = "CREATE TABLE excluded_records(id TEXT)"')
    monkeypatch.setattr(rehearsal, 'REPO', root)
    assert rehearsal.declared_tables() == {'canonical_records'}


def test_default_scratch_profiles_are_unique_and_use_temp_root(rehearsal, tmp_path, monkeypatch):
    copy = tmp_path / 'copy.db'
    copy.touch()
    monkeypatch.setattr(rehearsal.tempfile, 'tempdir', str(tmp_path))
    monkeypatch.setattr(rehearsal, 'snapshot', lambda _: {
        'size': 0, 'schema_version': 31, 'tables': {},
    })
    monkeypatch.setattr(rehearsal, 'declared_tables', set)
    monkeypatch.setattr(rehearsal, 'probes', lambda _: {})
    homes = []

    def open_copy(path, home, python):
        assert path == copy and home.is_dir()
        homes.append(home)
        return {'opened': True, 'exit': 0}

    monkeypatch.setattr(rehearsal, 'open_with_candidate', open_copy)
    monkeypatch.setattr(rehearsal, 'assess', lambda *args: {'ok': True})
    for _ in range(2):
        assert rehearsal.main([str(copy)]) == 0
    assert homes[0] != homes[1]
    assert all(home.parent == tmp_path for home in homes)


@pytest.mark.parametrize("existing", ["state", "symlink", "populated"])
def test_candidate_open_refuses_nonfresh_scratch_home(rehearsal, tmp_path, monkeypatch, existing):
    copy = tmp_path / "copy.db"
    copy.write_bytes(b"disposable copy")
    home = tmp_path / "scratch"
    home.mkdir()
    monkeypatch.setattr(rehearsal.tempfile, "tempdir", str(tmp_path))
    target = home / "state.db"
    if existing == "state":
        target.write_bytes(b"live profile database")
    elif existing == "symlink":
        target.symlink_to(copy)
    else:
        (home / "config.yaml").write_text("live profile", encoding="utf-8")
    monkeypatch.setattr(rehearsal.subprocess, "run", lambda *a, **kw: pytest.fail("spawned on nonfresh home"))
    with pytest.raises(SystemExit, match="fresh|state.db"):
        rehearsal.open_with_candidate(copy, home, "python")
    if existing == "state":
        assert target.read_bytes() == b"live profile database"
    elif existing == "symlink":
        assert target.is_symlink()
    assert copy.read_bytes() == b"disposable copy"


@pytest.mark.parametrize("location", ["equal", "ancestor", "nested", "symlink_nested"])
def test_candidate_open_refuses_live_home_or_its_ancestor(rehearsal, tmp_path, monkeypatch, location):
    live = tmp_path / "live-profile"
    live.mkdir()
    copy = tmp_path / "copy.db"
    copy.write_bytes(b"disposable copy")
    monkeypatch.setenv("HERMES_HOME", str(live))
    monkeypatch.setattr(rehearsal.tempfile, "tempdir", str(tmp_path))
    home = live if location == "equal" else tmp_path if location == "ancestor" else live / "scratch"
    if location == "symlink_nested":
        alias = tmp_path / "profile-alias"
        alias.symlink_to(live, target_is_directory=True)
        home = alias / "scratch"
    spawned = []

    def spawn(*args, **kwargs):
        spawned.append(args)
        return type("Result", (), {"returncode": 0, "stdout": '{"opened": true}', "stderr": ""})()

    monkeypatch.setattr(rehearsal.subprocess, "run", spawn)
    with pytest.raises(SystemExit, match="fresh scratch"):
        rehearsal.open_with_candidate(copy, home, "python")
    assert not spawned
    assert not (live / "scratch").exists()
    assert copy.read_bytes() == b"disposable copy"


@pytest.mark.parametrize("location", ["scratch_temp", "explicit_scratch", "nested_temp", "symlink_temp"])
def test_candidate_open_accepts_hermes_temp_scratch_home(rehearsal, v2_copy, tmp_path, monkeypatch, location):
    import sys
    live = tmp_path / "live-profile"
    scratch_root = live / "cache" / "scratch"
    live.mkdir()
    temp_root = live / "temporary" if location == "nested_temp" else scratch_root
    home = temp_root / "candidate"
    if location == "explicit_scratch":
        temp_root = tmp_path
    elif location == "symlink_temp":
        alias = tmp_path / "profile-alias"
        alias.symlink_to(live, target_is_directory=True)
        home = alias / "cache" / "scratch" / "candidate"
    monkeypatch.setenv("HERMES_HOME", str(live))
    monkeypatch.setenv("TMPDIR", str(temp_root))
    monkeypatch.setattr(rehearsal.tempfile, "tempdir", str(temp_root))
    result = rehearsal.open_with_candidate(v2_copy, home, sys.executable)
    assert result["opened"] and result["exit"] == 0, result
    assert (home / "state.db").resolve() == v2_copy.resolve()


def test_candidate_open_uses_fresh_home_and_real_candidate(rehearsal, v2_copy, tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(rehearsal.tempfile, "tempdir", str(tmp_path))
    home = tmp_path / "fresh-scratch"
    result = rehearsal.open_with_candidate(v2_copy, home, sys.executable)
    assert result["opened"] and result["exit"] == 0, result
    assert (home / "state.db").resolve() == v2_copy.resolve()


def test_rehearsal_refuses_active_custom_profile(rehearsal, tmp_path, monkeypatch):
    home = tmp_path / "custom-profile"
    home.mkdir()
    copy = home / "state.db"
    copy.touch()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(rehearsal, "snapshot", lambda p: pytest.fail("read live profile"))
    with pytest.raises(SystemExit, match="live profile"):
        rehearsal.main([str(copy)])


def test_invalid_copy_does_not_allocate_scratch_home(rehearsal, tmp_path, monkeypatch):
    def unexpected_allocation(**kwargs):
        pytest.fail('invalid input allocated a scratch profile')

    monkeypatch.setattr(rehearsal.tempfile, 'mkdtemp', unexpected_allocation)
    with pytest.raises(SystemExit, match='not a file'):
        rehearsal.main([str(tmp_path / 'missing.db')])
