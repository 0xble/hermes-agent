"""LOCAL check-ins use durable admissions and transcript replay, not a metadata-only restore."""
import os
from pathlib import Path
import subprocess
import sys


def test_local_checkin_restores_queued_descriptor_fences_crash_and_replays_visible_status(tmp_path):
    root = Path(__file__).resolve().parents[2]
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ', 'TIRITH_ENABLED') if k in os.environ}
    env.update(PYTHONPATH=str(root), HERMES_HOME=str(tmp_path / 'probe-home'),
               HERMES_TEST_ISOLATION=str(tmp_path / 'probe-isolation'))
    result = subprocess.run([
        sys.executable, str(root / 'tests/gateway/fixtures/local_supervisor_peer.py'), str(tmp_path)],
        cwd=root, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=180, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout)
