"""Parked recovery uses coordinator authority and a fully injected launchctl."""
import os
import plistlib
import subprocess

import pytest

from gateway.generation import GenerationCoordinator, GenerationIdentity
from hermes_cli import gateway_guardian as guardian


def layout(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    release = home / 'releases' / ('a' * 40)
    release.mkdir(parents=True)
    for marker in ('.release-ready', '.hermes_build_sha'):
        (release / marker).write_text(release.name, encoding="utf-8")
    (home / 'current').symlink_to(release)
    (home / 'config.yaml').write_text('gateway:\n  forward_only_handover:\n    enabled: true\n', encoding="utf-8")
    return home, release


def fake_launchctl(label, plist, home, *, pid=None, bootout_succeeds=True):
    calls = []
    state = {'loaded': True, 'rebootstrapped': False}
    def run(argv, **kwargs):
        calls.append(argv[1:])
        if argv[1] == 'print':
            loaded = state['loaded'] and argv[2] == f'gui/{os.getuid()}/{label}'  # windows-footgun: ok (macos_only caller)
            output = 'state = waiting\nlast exit code = 0\n'
            if pid or state['rebootstrapped']:
                output += f'pid = {pid or 789}\n'
            return subprocess.CompletedProcess(argv, 0 if loaded else 113,
                stdout=output if loaded else '', stderr='' if loaded else 'Could not find service')
        if argv[1] == 'bootout' and bootout_succeeds:
            state['loaded'] = False
        if argv[1] == 'bootstrap':
            state.update(loaded=True, rebootstrapped=True)
            assert plistlib.loads(plist.read_bytes())['EnvironmentVariables']['HERMES_GENERATION_SCOPE'] != 'old'
            # Retirement precedes launch actions and never releases the lease.
            db = GenerationCoordinator(home)
            assert all(row['state'] == 'exited' for row in db.generations() if row['label'] == label)
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')
    return run, calls, state


@pytest.mark.platforms("macos")
@pytest.mark.parametrize('case', ['dead', 'reboot', 'live', 'unknown', 'other-serving', 'drainer', 'running', 'bootout-fails', 'capped'])
def test_parked_repair_is_service_only_and_death_fenced(tmp_path, monkeypatch, case):
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'boot')
    home, release = layout(tmp_path)
    label = 'ai.hermes.gateway'
    db = GenerationCoordinator(home)
    old = GenerationIdentity.create(release_sha=release.name, label=label, pid=123,
        start_fingerprint='123:1', boot_id='prior' if case == 'reboot' else 'boot')
    db.register(old, state='serving')
    db.acquire_lease('active_generation', old.id)
    if case in {'drainer', 'other-serving'}:
        successor = GenerationIdentity.create(release_sha=release.name, label='successor',
                                             pid=456, boot_id='boot')
        db.register(successor)
        db.request_transfer(old.id, successor.id, 1, set())
        db.commit_transfer(old.id, successor.id, 1)
        if case == 'drainer':
            db.heartbeat(old.id, state='exited')
    monkeypatch.setattr('gateway.status._pid_exists', lambda pid: case in {'live', 'unknown', 'running'})
    monkeypatch.setattr('gateway.status._get_process_start_time', lambda pid: None if case == 'unknown' else 1)
    if case == 'drainer':
        release = home / 'releases' / ('b' * 40)  # Completed labels may pin a previous release.
    plist = tmp_path / 'service.plist'
    plist.write_bytes(plistlib.dumps({'Label': label, 'WorkingDirectory': str(release),
        'EnvironmentVariables': {'HERMES_HOME': str(home), 'HERMES_GENERATION_SCOPE': 'old'}}))
    runner, calls, state = fake_launchctl(label, plist, home, pid=123 if case == 'running' else None,
                                         bootout_succeeds=case != 'bootout-fails')
    monkeypatch.setattr(guardian, 'healthy', lambda *args, **kwargs: state['rebootstrapped'])
    if case == 'capped':
        for _ in range(guardian.MAX_REPAIRS):
            guardian.receipt(home, 'bootstrap', 'attempt', label=label)
    outcome = guardian.run_once(home, plist, label, grace=12, domain=f'gui/{os.getuid()}', launchctl_runner=runner)  # windows-footgun: ok (macos_only test)
    mutations = [call[0] for call in calls if call[0] in {'bootout', 'bootstrap'}]
    if case in {'dead', 'reboot'}:
        assert outcome == 'repaired' and mutations == ['bootout', 'bootstrap']
        retired = next(row for row in db.generations() if row['id'] == old.id)
        assert (retired['state'], retired['verdict'], retired['verdict_evidence']) == (
            'exited', 'failed', 'boot_changed' if case == 'reboot' else 'dead')
        assert db.leases()[0]['generation_id'] == old.id and db.leases()[0]['state'] == 'active'
        assert guardian._repair_count(home) == 1
    elif case == 'drainer':
        assert outcome == 'cleaned' and mutations == ['bootout']
        assert guardian._repair_count(home) == 0
    elif case == 'bootout-fails':
        assert outcome == 'alert' and mutations == ['bootout']
    elif case == 'capped':
        assert outcome == 'capped' and mutations == []
    else:
        assert outcome == 'waiting' and mutations == []


@pytest.mark.platforms("macos")
def test_health_probe_uses_injected_launchctl(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import json
    import psutil
    home, release = layout(tmp_path)
    (home / 'gateway_state.json').write_text(json.dumps({'pid': 123, 'gateway_state': 'running',
        'code_sha': release.name, 'updated_at': guardian.datetime.now(guardian.timezone.utc).isoformat()}),
        encoding='utf-8')
    monkeypatch.setattr(guardian.subprocess, 'run', lambda *args, **kwargs: pytest.fail('real launchctl'))
    calls = []
    def launchctl(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='"PID" = 123;', stderr='')
    monkeypatch.setattr(psutil, 'Process', lambda pid: SimpleNamespace(is_running=lambda: True,
        parents=lambda: [], cwd=lambda: str(release)))
    assert guardian.healthy(home, 'ai.hermes.gateway', release, launchctl)
    assert calls == [['launchctl', 'list', 'ai.hermes.gateway']]


@pytest.mark.platforms("macos")
def test_parked_repair_waits_for_documented_startup_budget(tmp_path, monkeypatch):
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'boot')
    home, release = layout(tmp_path)
    label = 'ai.hermes.gateway'
    db = GenerationCoordinator(home)
    old = GenerationIdentity.create(release_sha=release.name, label=label, pid=123,
        start_fingerprint='123:1', boot_id='boot')
    db.register(old, state='serving')
    db.acquire_lease('active_generation', old.id)
    monkeypatch.setattr('gateway.status._pid_exists', lambda pid: False)
    monkeypatch.setattr('gateway.status._get_process_start_time', lambda pid: 1)
    plist = tmp_path / 'service.plist'
    plist.write_bytes(plistlib.dumps({'Label': label, 'WorkingDirectory': str(release),
        'EnvironmentVariables': {'HERMES_HOME': str(home), 'HERMES_GENERATION_SCOPE': 'old'}}))
    runner, _calls, _state = fake_launchctl(label, plist, home)
    clock = [0.0]
    monkeypatch.setattr(guardian.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(guardian.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(guardian, 'healthy', lambda *args, **kwargs: clock[0] >= 13.0)

    assert guardian.run_once(home, plist, label, grace=12,
                            domain=f'gui/{os.getuid()}', launchctl_runner=runner) == 'repaired'


def test_bootstrap_repair_rejects_healthy_result_after_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'boot')
    home, release = layout(tmp_path)
    label = 'ai.hermes.gateway'
    db = GenerationCoordinator(home)
    old = GenerationIdentity.create(release_sha=release.name, label=label, pid=123,
        start_fingerprint='123:1', boot_id='boot')
    db.register(old, state='serving')
    db.acquire_lease('active_generation', old.id)
    monkeypatch.setattr('gateway.status._pid_exists', lambda pid: False)
    monkeypatch.setattr('gateway.status._get_process_start_time', lambda pid: 1)
    plist = tmp_path / 'service.plist'
    plist.write_bytes(plistlib.dumps({'Label': label, 'WorkingDirectory': str(release),
        'EnvironmentVariables': {'HERMES_HOME': str(home), 'HERMES_GENERATION_SCOPE': 'old'}}))
    runner, calls, state = fake_launchctl(label, plist, home)
    state['loaded'] = False
    timeouts = []
    real_launch_state = guardian._launch_state
    def bounded_launch_state(*args, **kwargs):
        if 'timeout' in kwargs:
            timeouts.append(kwargs['timeout'])
        return real_launch_state(*args, **kwargs)
    monkeypatch.setattr(guardian, '_launch_state', bounded_launch_state)
    clock = [0.0]
    monkeypatch.setattr(guardian.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(guardian.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def late_healthy(*args, **kwargs):
        clock[0] = guardian.STARTUP_SECONDS + .1
        return True

    monkeypatch.setattr(guardian, 'healthy', late_healthy)
    outcome = guardian.run_once(home, plist, label, grace=12,
                               domain=f'gui/{os.getuid()}', launchctl_runner=runner)
    assert outcome == 'failed'
    assert timeouts and all(0 < timeout <= guardian.STARTUP_SECONDS for timeout in timeouts)
    assert any(row[0] == 'print' for row in calls)


def test_parked_repair_passes_remaining_timeout_and_rejects_late_health(tmp_path, monkeypatch):
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'boot')
    home, release = layout(tmp_path)
    label = 'ai.hermes.gateway'
    db = GenerationCoordinator(home)
    old = GenerationIdentity.create(release_sha=release.name, label=label, pid=123,
        start_fingerprint='123:1', boot_id='boot')
    db.register(old, state='serving')
    db.acquire_lease('active_generation', old.id)
    monkeypatch.setattr('gateway.status._pid_exists', lambda pid: False)
    monkeypatch.setattr('gateway.status._get_process_start_time', lambda pid: 1)
    plist = tmp_path / 'service.plist'
    plist.write_bytes(plistlib.dumps({'Label': label, 'WorkingDirectory': str(release),
        'EnvironmentVariables': {'HERMES_HOME': str(home), 'HERMES_GENERATION_SCOPE': 'old'}}))
    runner, calls, _state = fake_launchctl(label, plist, home)
    timeouts = []
    real_launch_state = guardian._launch_state
    def bounded_launch_state(*args, **kwargs):
        if 'timeout' in kwargs:
            timeouts.append(kwargs['timeout'])
        return real_launch_state(*args, **kwargs)
    monkeypatch.setattr(guardian, '_launch_state', bounded_launch_state)
    clock = [0.0]
    monkeypatch.setattr(guardian.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(guardian.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def late_healthy(*args, **kwargs):
        clock[0] = guardian.STARTUP_SECONDS + .1
        return True

    monkeypatch.setattr(guardian, 'healthy', late_healthy)
    outcome = guardian._repair_parked(home, plist, label, f'gui/{os.getuid()}', release, runner)
    assert outcome == 'failed'
    assert timeouts and all(0 < timeout <= guardian.STARTUP_SECONDS for timeout in timeouts)
    assert any(argv[0] == 'print' for argv in calls)


def test_late_service_label_repair_inherits_the_original_startup_bound(tmp_path, monkeypatch):
    """The forward-only label-mismatch branch must reuse _run's STARTUP_SECONDS
    bound, not open a fresh one when entered late."""
    from gateway import deadline as gd
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'boot')
    home, release = layout(tmp_path)
    label = 'ai.hermes.gateway.g-other'
    plist = tmp_path / 'service.plist'
    plist.write_bytes(plistlib.dumps({'Label': label, 'WorkingDirectory': str(release),
        'EnvironmentVariables': {'HERMES_HOME': str(home), 'HERMES_GENERATION_SCOPE': 'old'}}))
    clock = [100.0]
    monkeypatch.setattr(gd, 'now', lambda: clock[0])
    seen = {}
    from gateway.generation import GenerationCoordinator
    def late_service_label(self):
        clock[0] += 40  # the label-mismatch branch is entered late
        return 'ai.hermes.gateway'
    monkeypatch.setattr(GenerationCoordinator, 'service_label', late_service_label)
    monkeypatch.setattr(guardian, '_gateway_domain', lambda *a, **k: f'gui/{os.getuid()}')
    monkeypatch.setattr(guardian, '_launch_state', lambda *a, **k: 'parked')
    def repair(*args, deadline=None, **kwargs):
        seen['deadline'] = deadline
        return 'repaired'
    monkeypatch.setattr(guardian, '_repair_parked', repair)
    assert guardian._run(home, plist, label, grace=0, domain=None, forward_only=True) == 'repaired'
    assert seen['deadline'] == 100.0 + guardian.STARTUP_SECONDS
