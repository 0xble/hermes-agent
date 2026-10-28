"""Rollback escalation is bounded, observable and fails closed on unknown ownership."""
import json
import subprocess

import psutil
import pytest

from hermes_cli import gateway_guardian as guardian
from tests.hermes_cli.test_gateway_guardian import layout


@pytest.mark.parametrize('remaining', [60, 8])
def test_rollback_kill_logs_owner_and_inflight_with_startup_budget(tmp_path, monkeypatch, caplog, remaining):
    (tmp_path / 'gateway_state.json').write_text(json.dumps({'pid': 456, 'active_agents': 2}))
    monkeypatch.setattr(guardian.time, 'monotonic', lambda: 100)
    waits = []

    class Process:
        pid = 456
        killed = False

        def wait(self, timeout):
            waits.append(timeout)
            if not self.killed:
                raise psutil.TimeoutExpired(timeout, self.pid)

        def kill(self):
            self.killed = True

    owner = Process()
    guardian._reap_rollback_owners([owner], 100 + remaining, home=tmp_path)
    assert owner.killed
    assert waits[0] == min(15, remaining / 4)
    assert any(record.levelname == 'WARNING' and 'pid=456' in record.message and
               'in-flight agents=2' in record.message for record in caplog.records)


@pytest.mark.platforms('macos')
@pytest.mark.parametrize('failure', ['timeout', 'oserror', 'not_running'])
def test_rollback_probe_failure_is_not_misattributed_to_foreign_owner(tmp_path, monkeypatch, failure):
    from gateway import status
    home, plist, label, previous, _ = layout(tmp_path)
    monkeypatch.setattr(status, 'live_gateway_pid_for_home', lambda target: 456)
    monkeypatch.setattr(psutil, 'Process', lambda pid: object())
    monkeypatch.setattr(guardian, 'rollback', lambda *args, **kwargs: kwargs['reload_callback']())
    calls = []

    def probe(argv, **kwargs):
        calls.append(argv)
        assert argv[:2] == ['launchctl', 'list'], 'unknown ownership must not bootout or bootstrap'
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
        if failure == 'oserror':
            raise OSError('probe unavailable')
        return subprocess.CompletedProcess(argv, 0, stdout='"PID" = 0;', stderr='')

    monkeypatch.setattr(guardian.subprocess, 'run', probe)
    expected = 'cannot probe launchd supervised PID' if failure != 'not_running' else 'no supervised PID'
    with pytest.raises(RuntimeError, match=expected):
        guardian.rollback_switch(home, plist, label, previous, domain='gui/fixture')
    assert len(calls) == 1
