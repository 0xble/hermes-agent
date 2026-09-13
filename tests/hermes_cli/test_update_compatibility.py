"""Managed source changes must never cross the admitted-result reader floor."""
import json
import subprocess
from pathlib import Path

import pytest
import shutil
import sys
import zipfile
from types import SimpleNamespace

from hermes_cli import update_cmd


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.email', 'fixture@example.invalid')
    git(tmp_path, 'config', 'user.name', 'Fixture')
    (tmp_path / 'reader.py').write_text('legacy = True\n', encoding="utf-8")
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'legacy')
    old = git(tmp_path, 'rev-parse', 'HEAD')
    (tmp_path / 'runtime-compatibility.json').write_text(json.dumps({
        'schema': 1, 'capabilities': ['delegation-admitted-v1', 'managed-downgrade-floor-v1']}), encoding="utf-8")
    (tmp_path / 'reader.py').write_text('legacy = False\n', encoding="utf-8")
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'compatible')
    return tmp_path, old, git(tmp_path, 'rev-parse', 'HEAD')


@pytest.mark.parametrize('command', [
    ['checkout', '--detach'], ['reset', '--hard'], ['merge', '--ff-only'],
])
def test_managed_source_change_refuses_incompatible_target(repo, command):
    root, old, current = repo
    with pytest.raises(RuntimeError, match='compatib'):
        update_cmd._git_run(['git'], [*command, old], root, check=True)
    assert git(root, 'rev-parse', 'HEAD') == current
    assert (root / 'reader.py').read_text(encoding="utf-8") == 'legacy = False\n'


def test_zip_refuses_incompatible_archive_before_swap(repo, tmp_path, monkeypatch):
    from hermes_cli.update_cmd_zip import _download_and_swap_zip
    root, _, current = repo
    archive = tmp_path.parent / (tmp_path.name + '.zip')
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('hermes-agent-main/reader.py', 'legacy = True\n')
    monkeypatch.setattr('urllib.request.urlretrieve', lambda url, dest: shutil.copyfile(archive, dest))
    monkeypatch.setattr(update_cmd, '_m', lambda: SimpleNamespace(PROJECT_ROOT=root, sys=sys))
    monkeypatch.setattr('hermes_cli.update_cmd_zip._require_staging_space', lambda *a: None)
    monkeypatch.setattr('hermes_cli.update_cmd_zip._zip_overlay_block_reason', lambda *a, **k: None)
    # Real ZIP extraction and replacement path, isolated repository.
    with pytest.raises(SystemExit):
        _download_and_swap_zip('main', 'https://fixture.invalid/source.zip')
    assert git(root, 'rev-parse', 'HEAD') == current
    assert (root / 'reader.py').read_text(encoding="utf-8") == 'legacy = False\n'


def test_stash_restore_cannot_replace_reader_with_unverified_local_code(repo):
    from hermes_cli.update_cmd_stash import _apply_stash
    root, old, current = repo
    (root / 'reader.py').write_text('legacy = True\n', encoding="utf-8")
    git(root, 'stash', 'push', '-qm', 'unsafe code')
    with pytest.raises(RuntimeError, match='compatib'):
        _apply_stash(['git'], root, 'stash@{0}')
    assert git(root, 'rev-parse', 'HEAD') == current
    assert (root / 'reader.py').read_text(encoding="utf-8") == 'legacy = False\n'



@pytest.mark.parametrize('state', ['pending', 'admitted', 'delivered'])
def test_floor_does_not_depend_on_profile_counts(repo, tmp_path, monkeypatch, state):
    import sqlite3
    root, old, current = repo
    for home in (tmp_path / 'home', tmp_path / 'home/profiles/other'):
        home.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(home / 'state.db') as db:
            db.execute('CREATE TABLE async_delegations(delivery_state TEXT)')
            db.execute('INSERT INTO async_delegations VALUES (?)', (state,))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    # No permission to downgrade can come from an empty/missing/failed DB read.
    with pytest.raises(RuntimeError, match='compatib'):
        update_cmd._git_run(['git'], ['reset', '--hard', old], root)
    assert git(root, 'rev-parse', 'HEAD') == current


def test_compatible_update_pins_ref_before_concurrent_admission(repo, monkeypatch):
    from hermes_cli import update_compatibility as guard
    import sqlite3
    root, old, current = repo
    (root / 'reader.py').write_text('legacy = False\nnew = True\n', encoding="utf-8")
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'next compatible')
    target = git(root, 'rev-parse', 'HEAD')
    git(root, 'branch', 'next', target)
    git(root, 'checkout', '--detach', current)
    original = guard.require_git_target
    def checked(*args):
        sha = original(*args)
        # Simulate a new admission after the check and a mutable ref moving.
        with sqlite3.connect(root / 'fixture.db') as db:
            db.execute('CREATE TABLE async_delegations(delivery_state TEXT)')
            db.execute("INSERT INTO async_delegations VALUES ('admitted')")
        git(root, 'update-ref', 'refs/heads/next', old)
        return sha
    monkeypatch.setattr(guard, 'require_git_target', checked)
    update_cmd._git_run(['git'], ['merge', '--ff-only', 'next'], root, check=True)
    assert git(root, 'rev-parse', 'HEAD') == target


def test_compatible_downgrade_retains_two_hop_floor(repo):
    root, old, compatible = repo
    (root / 'reader.py').write_text('legacy = False\nnew = True\n', encoding="utf-8")
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'next compatible')
    update_cmd._git_run(['git'], ['checkout', '--detach', compatible], root, check=True)
    assert git(root, 'rev-parse', 'HEAD') == compatible
    with pytest.raises(RuntimeError, match='compatib'):
        update_cmd._git_run(['git'], ['checkout', '--detach', old], root)


@pytest.mark.parametrize('manifest', [
    '{}', 'null', '[]', '{broken',
    '{"schema":true,"capabilities":["delegation-admitted-v1","managed-downgrade-floor-v1"]}',
    '{"schema":1,"capabilities":["delegation-admitted-v1"]}',
])
def test_invalid_or_guardless_targets_fail_closed(repo, manifest):
    root, _, current = repo
    (root / 'runtime-compatibility.json').write_text(manifest, encoding="utf-8")
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'invalid target')
    invalid = git(root, 'rev-parse', 'HEAD')
    git(root, 'checkout', '--detach', current)
    with pytest.raises(RuntimeError, match='compatib'):
        update_cmd._git_run(['git'], ['reset', '--hard', invalid], root)
    assert git(root, 'rev-parse', 'HEAD') == current


def test_syntax_rollback_cannot_reinstall_old_reader_after_partial_activation(repo, monkeypatch):
    root, old, current = repo
    monkeypatch.setattr(update_cmd, '_m', lambda: SimpleNamespace(PROJECT_ROOT=root))
    monkeypatch.setattr(update_cmd, '_validate_critical_files_syntax', lambda root: (False, 'reader.py', 'synthetic'))
    with pytest.raises(RuntimeError, match='compatib'):
        update_cmd._rollback_if_pulled_syntax_error(['git'], old)
    assert git(root, 'rev-parse', 'HEAD') == current


def test_failed_capability_read_never_mutates(repo, monkeypatch):
    from hermes_cli import update_compatibility as guard
    root, _, current = repo
    real = guard.subprocess.run
    def unreadable(cmd, **kw):
        if 'show' in cmd:
            raise PermissionError('fixture')
        return real(cmd, **kw)
    monkeypatch.setattr(guard.subprocess, 'run', unreadable)
    with pytest.raises(RuntimeError, match='compatib'):
        update_cmd._git_run(['git'], ['reset', '--hard', current], root)
    assert git(root, 'rev-parse', 'HEAD') == current
