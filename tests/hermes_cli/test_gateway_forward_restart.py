"""Forward-only restarts must load a new supervisor scope, using fake launchctl."""
import os
import plistlib
import subprocess
import uuid
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig
from gateway.generation import GenerationCoordinator, GenerationIdentity
from hermes_cli import gateway, gateway_launchd as launch


@pytest.fixture(params=['legacy', 'generation'])
def service(tmp_path, monkeypatch, request):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'boot')
    monkeypatch.setenv('HERMES_GENERATION_SCOPE', 'consumed')
    label = 'ai.hermes.gateway' if request.param == 'legacy' else f'ai.hermes.gateway.g-{uuid.uuid4().hex}'
    monkeypatch.setenv('HERMES_LAUNCHD_LABEL', label)
    (tmp_path / 'config.yaml').write_text('gateway:\n  forward_only_handover:\n    enabled: true\n', encoding='utf-8')
    legacy_plist = tmp_path / 'ai.hermes.gateway.plist'
    plist = tmp_path / f'{label}.plist'
    plist.write_bytes(plistlib.dumps({'Label': label, 'RunAtLoad': True,
        'EnvironmentVariables': {'HERMES_HOME': str(tmp_path), 'HERMES_GENERATION_SCOPE': 'consumed'}}))
    legacy_plist.write_bytes(plist.read_bytes())
    monkeypatch.setattr(gateway, 'get_launchd_plist_path', lambda: legacy_plist)
    monkeypatch.setattr(gateway, 'get_launchd_label', lambda: 'ai.hermes.gateway')
    monkeypatch.setattr(gateway, '_launchd_domain', lambda: 'gui/fixture')
    monkeypatch.setattr('gateway.status.get_running_pid', lambda: None)
    monkeypatch.setattr(gateway, 'refresh_launchd_plist_if_needed', lambda: False)
    monkeypatch.setattr(gateway, '_clear_launchd_unsupported_marker', lambda: None)
    monkeypatch.setattr(gateway, '_wait_for_api_server_port_free', lambda: None)
    monkeypatch.setattr('hermes_cli.gateway_guardian.set_intent', lambda *a, **k: None)
    db = GenerationCoordinator(tmp_path)
    if request.param == 'generation':
        db.reserve_generation(release_sha='r', label=label, boot_id='boot')
    process = GenerationIdentity.create(release_sha='r', label=label, boot_id='boot',
                                         pid=os.getpid(), start_fingerprint='old')
    identity = db.claim_process(process, 'consumed')
    epoch = db.acquire_lease('active_generation', identity.id)
    db.transition_state(identity.id, 'standby', 'serving')
    db.release_lease('active_generation', identity.id, epoch)
    db.transition_state(identity.id, 'serving', 'exited')
    calls, successors = [], []
    def command(args, **kwargs):
        calls.append(args)
        if args[1] == 'bootstrap':
            scope = plistlib.loads(plist.read_bytes())['EnvironmentVariables']['HERMES_GENERATION_SCOPE']
            child = GenerationIdentity.create(release_sha='r', label=process.label, boot_id='boot',
                pid=os.getpid()+1, start_fingerprint='new')
            successor = db.claim_process(child, scope)
            assert successor is not None, 'respawn parked in consumed scope'
            successors.append(successor)
        return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
    monkeypatch.setattr(launch.subprocess, 'run', command)
    monkeypatch.setattr(gateway, '_launchctl_label_supervising_process', lambda label: bool(successors))
    return plist, calls, successors, command


@pytest.mark.platforms("macos")
@pytest.mark.parametrize('action', ['start', 'restart'])
def test_loaded_service_launch_opens_fresh_scope(service, action):
    plist, calls, successors, _ = service
    getattr(launch, f'launchd_{action}')()
    assert plistlib.loads(plist.read_bytes())['EnvironmentVariables']['HERMES_GENERATION_SCOPE'] != 'consumed'
    assert [call[1] for call in calls] == ['bootout', 'bootstrap']
    assert len(successors) == 1


@pytest.mark.platforms("macos")
@pytest.mark.parametrize('explicit_exit', [75, None])
def test_planned_exit_hands_off_reload_before_parking(service, monkeypatch, explicit_exit):
    from gateway.run_shutdown import _resolve_gateway_exit_verdict
    plist, calls, successors, command = service
    # Execute the real helper submission through a fake launchctl interpreter.
    def submit(args, **kwargs):
        if args[1] == 'submit':
            calls.append(args)
            script = args[-1]
            assert 'launchctl bootout' in script and 'while kill -0' in script
            assert 'launchctl bootstrap' in script
            command(['launchctl', 'bootout', f'gui/fixture/{plist.stem}'])
            command(['launchctl', 'bootstrap', 'gui/fixture', str(plist)])
            return subprocess.CompletedProcess(args, 0)
        return command(args, **kwargs)
    monkeypatch.setattr(launch.subprocess, 'run', submit)
    runner = SimpleNamespace(config=GatewayConfig.from_dict({'gateway': {'forward_only_handover': {'enabled': True}}}),
        should_exit_with_failure=False, exit_code=explicit_exit, _restart_requested=True, _restart_via_service=True)
    assert _resolve_gateway_exit_verdict(runner, False) is True
    assert [call[1] for call in calls] == ['submit', 'bootout', 'bootstrap']
    assert len(successors) == 1
