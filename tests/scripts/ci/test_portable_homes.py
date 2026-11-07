"""Portable HOME ownership survives abrupt process death without sharing test state."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import pytest

from scripts.ci import portable as ci


def sweep(parent):
    with ci.external_temporary_directory('hermes-ci-home-', parent=parent):
        pass


def marked_home(parent, *, pid, start):
    home = parent / 'hermes-ci-home-orphan'
    home.mkdir()
    (home / '.owner.json').write_text(json.dumps({
        'pid': pid, 'process_start': start, 'host_id': 'fixture-host',
    }), encoding='utf-8')
    (home / 'payload').write_bytes(b'x' * 8192)
    return home


def test_sweep_reclaims_dead_owner_and_reports_bytes(tmp_path, capsys):
    home = marked_home(tmp_path, pid=2**30, start='dead')
    sweep(tmp_path)
    assert not home.exists()
    output = capsys.readouterr().out
    match = re.search(r'reclaimed 1 home\(s\), (\d+) allocated bytes', output)
    assert match and int(match[1]) >= 8192


def test_sweep_preserves_live_locked_home_and_publishes_identity(tmp_path):
    with ci.external_temporary_directory('hermes-ci-home-', parent=tmp_path) as home:
        owner = json.loads((home / '.owner.json').read_text(encoding='utf-8-sig'))
        assert owner['pid'] == os.getpid()
        assert owner['process_start']
        assert owner['host_id']
        # Even a corrupt/dead PID must not override a held kernel lock.
        (home / '.owner.json').write_text('{"pid": 1073741824}', encoding='utf-8')
        sweep(tmp_path)
        assert home.is_dir()
    assert not home.exists()


@pytest.mark.parametrize('age,child_age,kept', [(0, 0, True), (90000, 0, True), (90000, 90000, False)])
def test_legacy_age_includes_top_level_entries(tmp_path, age, child_age, kept):
    home = tmp_path / 'hermes-ci-home-legacy'
    home.mkdir()
    child = home / 'environments'
    child.mkdir()
    now = time.time()
    os.utime(child, (now - child_age, now - child_age))
    os.utime(home, (now - age, now - age))
    sweep(tmp_path)
    assert home.exists() is kept


def test_reused_live_pid_does_not_keep_unlocked_home(tmp_path):
    home = marked_home(tmp_path, pid=os.getpid(), start='different-process-start')
    sweep(tmp_path)
    assert not home.exists()


@pytest.mark.platforms('posix')
def test_sweep_never_follows_links_or_walks_nonmatching_children(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'sentinel'
    sentinel.write_text('keep', encoding='utf-8')
    (tmp_path / 'hermes-ci-home-link').symlink_to(outside, target_is_directory=True)
    home = marked_home(tmp_path, pid=2**30, start='dead')
    (home / 'escape').symlink_to(outside, target_is_directory=True)
    nested = outside / 'hermes-ci-home-nested'
    nested.mkdir()
    sweep(tmp_path)
    assert sentinel.read_text(encoding='utf-8-sig') == 'keep'
    assert nested.is_dir()
    assert (tmp_path / 'hermes-ci-home-link').is_symlink()
    assert not home.exists()


@pytest.mark.platforms('posix')
def test_sigkill_home_is_reclaimed_by_next_real_wrapper(tmp_path):
    code = '''
import sys
from pathlib import Path
from scripts.ci.portable import external_temporary_directory
with external_temporary_directory('hermes-ci-home-', parent=Path(sys.argv[1])) as home:
    (home / 'payload').write_bytes(b'x' * 16384)
    print(home, file=sys.stderr, flush=True)
    sys.stdin.read()
'''
    with subprocess.Popen([sys.executable, '-c', code, str(tmp_path)], cwd=ci.ROOT,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          encoding='utf-8') as owner:
        try:
            home = Path(owner.stderr.readline().strip())
            assert home.parent == tmp_path
            owner.kill()
            assert owner.wait(timeout=10) == -signal.SIGKILL
            assert home.is_dir(), 'SIGKILL must leave an orphan for the next invocation'
            next_run = subprocess.run(
                [sys.executable, '-c', code, str(tmp_path)], cwd=ci.ROOT, input='',
                capture_output=True, text=True, encoding='utf-8', timeout=20, check=False,
            )
            assert next_run.returncode == 0, next_run.stderr
            assert not home.exists()
            assert 'reclaimed 1 home' in next_run.stdout
            print(f'SIGKILL={owner.returncode}; orphan={home.name}; {next_run.stdout.strip()}')
        finally:
            if owner.poll() is None:
                owner.kill()
                owner.wait(timeout=10)
