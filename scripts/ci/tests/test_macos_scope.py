"""Which ready PRs need the pre-merge macOS job, on a real Git repository."""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('macos_scope', Path(__file__).resolve().parents[1] / 'macos_scope.py')
macos_scope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(macos_scope)


def git(root: Path, *args: str) -> None:
    subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    git(tmp_path, 'init', '-q', '-b', 'main')
    git(tmp_path, 'config', 'user.email', 'ci@example.invalid')
    git(tmp_path, 'config', 'user.name', 'ci')
    (tmp_path / 'agent').mkdir()
    (tmp_path / 'agent/core.py').write_text('X = 1\n')
    git(tmp_path, 'add', '-A')
    git(tmp_path, 'commit', '-qm', 'base')
    git(tmp_path, 'switch', '-qc', 'change')
    monkeypatch.chdir(tmp_path)
    return tmp_path


def commit(root: Path, path: str, text: str) -> None:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text)
    git(root, 'add', path)
    git(root, 'commit', '-qm', 'change')


def test_fork_sync_candidates_always_run(repo: Path) -> None:
    assert macos_scope.decide('sync/controller-abc-r1', 'main')[0] is True


@pytest.mark.parametrize(('path', 'text'), [
    ('gateway/platforms/macos_keychain.py', 'X = 1\n'),
    ('tools/launchd_helper.py', 'X = 1\n'),
    ('agent/core.py', 'import sys\nIS_MAC = sys.platform == "darwin"\n'),
    ('tests/test_tray.py', '@pytest.mark.platforms("macos")\ndef test_tray():\n    pass\n'),
    ('.github/workflows/gate.yml', 'name: gate\n'),
    ('uv.lock', 'version = 1\n'),
])
def test_macos_touching_changes_run(repo: Path, path: str, text: str) -> None:
    commit(repo, path, text)
    run, reason = macos_scope.decide('feat/x', 'main')
    assert run is True, reason


def test_linux_only_change_skips(repo: Path) -> None:
    commit(repo, 'agent/core.py', 'X = 2\n')
    assert macos_scope.decide('feat/x', 'main') == (False, 'no macOS-specific change')


def test_unreadable_diff_runs_to_be_safe(repo: Path) -> None:
    run, reason = macos_scope.decide('feat/x', 'no-such-ref')
    assert run is True
    assert 'running to be safe' in reason
