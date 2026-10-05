"""Supervisor scopes are plist data, never real launchctl actions in this suite."""
import plistlib
import uuid

import pytest

from hermes_cli.gateway_launchd_generation import render_generation_launchd_plist


@pytest.mark.parametrize('definition', [
    None,
    b'not a plist',
    b'<plist><key>orphan</key><string>value</string></plist>',
    b'<plist><integer>bad</integer></plist>',
    b'<plist><date>invalid</date></plist>',
    b'<plist><dict>',
    b'<plist><dict><key>x</key><dict><key>y</key></dict></dict></plist>',
])
def test_unreadable_plist_keeps_legacy_bootstrap_policy(tmp_path, monkeypatch, definition):
    from hermes_cli import gateway_launchd

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    # An unrelated invoking profile cannot opt a missing/broken definition in.
    (tmp_path / 'config.yaml').write_text(
        'gateway:\n  forward_only_handover:\n    enabled: true\n', encoding='utf-8')
    path = tmp_path / 'service.plist'
    if definition is not None:
        path.write_bytes(definition)
    assert gateway_launchd._forward_only_plist(path) is False


@pytest.mark.parametrize('config', [None, 'gateway: [', 'unreadable'])
def test_unreadable_config_keeps_legacy_bootstrap_policy(tmp_path, monkeypatch, config):
    from hermes_cli import gateway_launchd

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    path = tmp_path / 'service.plist'
    path.write_bytes(plistlib.dumps({'EnvironmentVariables': {'HERMES_HOME': str(tmp_path)}}))
    config_path = tmp_path / 'config.yaml'
    if config == 'unreadable':
        config_path.mkdir()  # A real read failure, independent of uid/permissions.
    elif config is not None:
        config_path.write_text(config, encoding='utf-8')
    assert gateway_launchd._forward_only_plist(path) is False


def test_generation_plist_has_fresh_scope_for_each_bootstrap_definition(tmp_path):
    release = tmp_path / 'release'
    python = release / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.touch()
    (tmp_path / 'config.yaml').write_text('gateway:\n  forward_only_handover:\n    enabled: true\n', encoding="utf-8")
    generation = uuid.uuid4()
    args = dict(slot=str(generation), release_sha='r', release_root=release, interpreter=python, hermes_home=tmp_path)
    first, second = [plistlib.loads(render_generation_launchd_plist(**args).encode()) for _ in range(2)]
    scope = first['EnvironmentVariables']['HERMES_GENERATION_SCOPE']
    assert scope and scope != second['EnvironmentVariables']['HERMES_GENERATION_SCOPE']
    # A stored standby definition must not start at login (SuccessfulExit implies RunAtLoad).
    assert (first['RunAtLoad'], first['KeepAlive']) == (False, False)
    holder = plistlib.loads(render_generation_launchd_plist(**args, standby=False).encode())
    assert (holder['RunAtLoad'], holder['KeepAlive']) == (True, {'SuccessfulExit': False})
    assert first['Label'] == f'ai.hermes.gateway.g-{generation.hex}'
    with pytest.raises(ValueError, match='UUID'):
        render_generation_launchd_plist(**{**args, 'slot': 'a'})


def test_refresh_scope_changes_only_bootstrap_nonce(tmp_path, monkeypatch):
    from hermes_cli import gateway_launchd_generation as launch
    path = tmp_path / 'generation.plist'
    payload = {'Label': 'ai.hermes.gateway', 'ProgramArguments': ['pinned'],
               'EnvironmentVariables': {'HERMES_HOME': str(tmp_path), 'HERMES_GENERATION_SCOPE': 'old'}}
    path.write_bytes(plistlib.dumps(payload))
    launch.refresh_generation_scope(path)
    first = plistlib.loads(path.read_bytes())
    assert first['EnvironmentVariables']['HERMES_GENERATION_SCOPE'] != 'old'
    launch.refresh_generation_scope(path)
    second = plistlib.loads(path.read_bytes())
    assert first['EnvironmentVariables']['HERMES_GENERATION_SCOPE'] != second['EnvironmentVariables']['HERMES_GENERATION_SCOPE']
    second['EnvironmentVariables']['HERMES_GENERATION_SCOPE'] = 'old'
    assert second == payload
    from types import SimpleNamespace
    from hermes_cli import gateway_launchd
    # A staged definition owns its policy even without an invoking CLI facade.
    monkeypatch.setattr(gateway_launchd, '_gw', lambda: SimpleNamespace())
    config = tmp_path / 'config.yaml'
    config.write_text('gateway:\n  forward_only_handover:\n    enabled: true\n', encoding='utf-8')
    assert gateway_launchd._forward_only_plist(path)
    config.write_text('gateway:\n  forward_only_handover:\n    enabled: false\n', encoding='utf-8')
    assert not gateway_launchd._forward_only_plist(path)


@pytest.mark.platforms("macos")
def test_service_plist_pins_release_and_ignores_nonce_for_staleness(tmp_path, monkeypatch):
    from hermes_cli import gateway
    home = tmp_path / 'profile'
    release = home / 'releases' / ('a' * 40)
    python = release / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.touch()
    (home / 'current').symlink_to(release)
    config = home / 'config.yaml'
    config.write_text('gateway:\n  forward_only_handover:\n    enabled: true\n', encoding="utf-8")
    monkeypatch.setattr(gateway, 'get_hermes_home', lambda: home)
    monkeypatch.setattr(gateway, 'get_launchd_label', lambda: 'ai.hermes.gateway')
    monkeypatch.setattr(gateway, '_build_service_path_dirs', lambda: [])
    monkeypatch.setattr(gateway, '_append_node_dir_for_service', lambda paths: None)
    plist = tmp_path / 'service.plist'
    monkeypatch.setattr(gateway, 'get_launchd_plist_path', lambda: plist)
    first, second = [gateway.generate_launchd_plist(release_target=release) for _ in range(2)]
    payload = plistlib.loads(first.encode())
    env = payload['EnvironmentVariables']
    assert env['HERMES_GENERATION_SCOPE'] != plistlib.loads(second.encode())['EnvironmentVariables']['HERMES_GENERATION_SCOPE']
    assert env['HERMES_RELEASE_SHA'] == release.name and env['PYTHONPATH'] == str(release)
    assert payload['WorkingDirectory'] == str(release)
    assert str(python) in str(payload['ProgramArguments'])
    plist.write_text(first, encoding="utf-8")
    assert gateway.launchd_plist_is_current(release_target=release)
    config.write_text('gateway:\n  forward_only_handover:\n    enabled: false\n', encoding="utf-8")
    legacy = plistlib.loads(gateway.generate_launchd_plist(release_target=release).encode())
    assert 'HERMES_GENERATION_SCOPE' not in legacy['EnvironmentVariables']


def test_forward_generation_launch_skips_host_attach(monkeypatch, tmp_path):
    """A KeepAlive respawn of a dead generation label must reach its coordinator claim.

    The host-attach guard would see the live successor serving this profile and exit 75,
    so launchd would respawn the label forever instead of parking it on exit 0.
    """
    from hermes_cli import gateway as gateway_cli
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    attached = []
    monkeypatch.setattr(gateway_cli, '_guard_official_docker_root_gateway', lambda: None)
    monkeypatch.setattr(gateway_cli, '_attach_to_host_gateway_or_guard',
                        lambda **kwargs: attached.append(kwargs))
    class ReachedCoordinatorRoute(Exception):
        pass
    def next_guard(**kwargs):
        raise ReachedCoordinatorRoute
    monkeypatch.setattr(gateway_cli, '_guard_supervised_gateway_conflict', next_guard)
    for enabled, scoped in ((True, True), (True, False), (False, True)):
        (tmp_path / 'config.yaml').write_text(
            f'gateway:\n  forward_only_handover:\n    enabled: {str(enabled).lower()}\n', encoding='utf-8')
        if scoped:
            monkeypatch.setenv('HERMES_GENERATION_SCOPE', 'scope')
        else:
            monkeypatch.delenv('HERMES_GENERATION_SCOPE', raising=False)
        attached.clear()
        with pytest.raises(ReachedCoordinatorRoute):
            gateway_cli.run_gateway()
        assert bool(attached) is not (enabled and scoped)
