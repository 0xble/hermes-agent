"""A launchd wrapper exiting is not proof its gateway released host/profile ownership."""
import json
import os
import subprocess
from types import SimpleNamespace

import psutil
import pytest

from hermes_cli import gateway_guardian as guardian
from tests.hermes_cli.test_gateway_guardian import layout


@pytest.mark.platforms('macos')
@pytest.mark.parametrize('wedged', [False, True])
def test_rollback_marks_and_reaps_real_gateway_below_wrapper(tmp_path, monkeypatch, wedged):
    from gateway import status
    from hermes_cli import gateway_launchd
    home, plist, label, previous, _ = layout(tmp_path)
    calls = []
    monkeypatch.setattr(gateway_launchd, '_launchctl_supervised_pid', lambda *args, **kwargs: 123)
    monkeypatch.setattr(status, 'live_gateway_pid_for_home', lambda target: 456 if target == home else None)

    class Process:
        def __init__(self, pid):
            self.pid = pid
            self.killed = False

        def parents(self):
            return [SimpleNamespace(pid=123)]

        def wait(self, timeout):
            calls.append(('wait', self.pid))
            if self.pid == 456 and wedged and not self.killed:
                raise psutil.TimeoutExpired(timeout, self.pid)

        def kill(self):
            calls.append(('kill', self.pid))
            self.killed = True

    monkeypatch.setattr(psutil, 'Process', Process)

    def run(argv, **kwargs):
        calls.append((argv[1], None))
        if argv[1] == 'bootout':
            marker = json.loads((home / '.gateway-planned-restart.json').read_text())
            assert marker['target_pid'] == 456  # never the launchd wrapper
        if argv[1] == 'bootstrap':
            assert ('wait', 456) in calls
            assert (('kill', 456) in calls) is wedged
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    monkeypatch.setattr(guardian.subprocess, 'run', run)
    monkeypatch.setattr(guardian, 'rollback', lambda *args, **kwargs:
                        (kwargs['reload_callback'](), {'reload_pending': False})[1])
    monkeypatch.setattr(guardian, 'healthy', lambda *args: True)
    assert guardian.rollback_switch(home, plist, label, previous, domain=f'gui/{os.getuid()}')
    assert calls[-1] == ('bootstrap', None)
    assert not any(action == 'kill' and pid == 123 for action, pid in calls)


def test_rollback_will_not_signal_a_gateway_from_another_service(tmp_path, monkeypatch):
    from gateway import status
    monkeypatch.setattr(status, 'live_gateway_pid_for_home', lambda home: 456)
    monkeypatch.setattr(psutil, 'Process', lambda pid: SimpleNamespace(
        pid=pid, parents=lambda: [SimpleNamespace(pid=999)]))
    with pytest.raises(RuntimeError, match='not owned'):
        guardian._rollback_owners(tmp_path, 123)


@pytest.mark.skipif(os.name == 'nt', reason='fcntl ownership proof is POSIX')
def test_reap_releases_real_host_lock_before_replacement(tmp_path, monkeypatch):
    import fcntl
    import sys
    import time
    lock = tmp_path / 'host.lock'
    child = subprocess.Popen([sys.executable, '-c',
        'import fcntl,sys,time; f=open(sys.argv[1],"w"); '
        'fcntl.flock(f,fcntl.LOCK_EX); print("owned",flush=True); time.sleep(360)', str(lock)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'owned'
        owner = psutil.Process(child.pid)
        monkeypatch.setattr(guardian, 'ROLLBACK_DRAIN_SECONDS', .05)
        start = time.monotonic()
        guardian._reap_rollback_owners([owner], start + 3)
        assert time.monotonic() < start + 3
        assert not owner.is_running()
        with lock.open('a') as replacement:
            fcntl.flock(replacement, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=3)
        child.stdout.close()
