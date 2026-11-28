"""Completion producers must commit to the same FIFO as human input."""
import os
from pathlib import Path
import subprocess
import sys
import time


_PROBE_TIMEOUT = 200
# Relay close and owner reap can each take 10s; leave another 5s for
# diagnostics, model shutdown, and other fixture exit overhead.
_TEARDOWN_MARGIN = 25


def _probe(tmp_path, mode):
    root = Path(__file__).resolve().parents[2]
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ', 'TIRITH_ENABLED', 'HOME', 'USERPROFILE',
                                    'TMPDIR', 'HERMES_TEST_SCRATCH_ROOT') if k in os.environ}
    env.update(PYTHONPATH=str(root), HERMES_HOME=str(tmp_path / 'probe-home'),
               HERMES_TEST_ISOLATION=str(tmp_path / 'probe-isolation'))
    deadline = time.monotonic() + _PROBE_TIMEOUT - _TEARDOWN_MARGIN
    result = subprocess.run([sys.executable, str(root / 'tests/gateway/fixtures/automation_peer.py'),
                             str(tmp_path), mode, str(deadline)],
                            env=env, cwd=root, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=_PROBE_TIMEOUT, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout)


def test_terminal_completion_commits_while_ordinary_daemon_model_is_held(tmp_path):
    _probe(tmp_path, 'terminal')


def test_heartbeat_timer_commits_while_ordinary_daemon_model_is_held(tmp_path):
    _probe(tmp_path, 'heartbeat')


def test_started_completion_is_interrupted_without_replay_after_owner_kill(tmp_path):
    _probe(tmp_path, 'restart-started')


def test_owner_replacement_rejects_stale_relay_provenance(tmp_path):
    _probe(tmp_path, 'restart-denied')
