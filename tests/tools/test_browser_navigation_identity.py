"""Navigation must retain a task's durable cookie jar and isolate private sidecars."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli.browser_identity import browser_identity_scope_key, read_browser_identity_config, resolve_browser_identity
from hermes_constants import hermes_home_key
from tests.tools.test_browser_identity import _browser_cfg
from tools import browser_tool as bt
from tools import browser_tool_session as sessions
from tools import browser_tool_cloud as cloud
from tools import browser_tool_cdp as cdp


@pytest.fixture
def browser_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    config = _browser_cfg()
    monkeypatch.setattr('hermes_cli.browser_identity.read_browser_identity_config', lambda: config)
    monkeypatch.setattr(bt, '_active_sessions', {})
    monkeypatch.setattr(bt, '_last_active_session_key', {})
    monkeypatch.setattr(bt, '_suspect_browser_sessions', {})
    monkeypatch.setattr(bt, '_is_camofox_mode', lambda: False)
    monkeypatch.setattr(bt, '_use_real_profile', lambda: True)
    monkeypatch.setattr(bt, '_start_browser_cleanup_thread', lambda: None)
    monkeypatch.setattr(bt, '_update_session_activity', lambda task: None)
    monkeypatch.setattr(bt, '_maybe_start_recording', lambda task: None)
    monkeypatch.setattr(bt, '_session_has_expired', lambda info: False)
    monkeypatch.setattr(bt, '_browser_session_backend', lambda task: SimpleNamespace(ensure_healthy=lambda: True))
    monkeypatch.setattr(cdp, '_ensure_cdp_supervisor', lambda task: None)
    monkeypatch.setattr(cdp, '_get_cdp_override_raw', lambda: None)
    monkeypatch.setattr(bt, '_get_cdp_override', lambda: None)
    monkeypatch.setattr(cloud, '_get_cloud_provider', lambda: None)
    monkeypatch.setattr(bt, '_get_cloud_provider', lambda: None)
    monkeypatch.setattr(cloud, '_is_local_backend', lambda: True)
    created, commands = [], []

    def create(task, allow_real_profile=True, identity=None):
        created.append((task, allow_real_profile, identity))
        info = {'session_name': 'fake-' + task, 'cdp_url': 'ws://127.0.0.1:9999' if identity else None,
                'bb_session_id': None, 'features': {'local': True, 'real_profile': bool(identity)}}
        if identity:
            resolved = resolve_browser_identity(identity, browser_cfg=config)
            info.update(browser_identity=identity, browser_identity_key=browser_identity_scope_key(resolved.runtime_key),
                        browser_identity_home=hermes_home_key())
        return info

    def command(task, action, args=None, **kwargs):
        commands.append((task, action))
        return {'success': True, 'data': {'title': 'Fixture', 'snapshot': 'Fixture', 'refs': {}}}

    monkeypatch.setattr(bt, '_create_local_session', create)
    monkeypatch.setattr(sessions, '_run_browser_command', command)
    router = Mock(side_effect=AssertionError('named/sidecar task escaped to extension routing'))
    monkeypatch.setattr(bt, 'routed_browser_handler', router)
    return config, created, commands, router


def navigate(entrypoint, *, task='named', identity=None, url='https://93.184.216.34/'):
    if entrypoint == 'registered':
        args = {'url': url}
        if identity is not None:
            args['identity'] = identity
        return json.loads(bt._browser_navigate_handler(args, {'task_id': task}))
    return json.loads(bt.browser_navigate(url, task_id=task, identity=identity))


@pytest.mark.parametrize('entrypoint', ['direct', 'registered'])
@pytest.mark.parametrize('required,restart', [(False, False), (False, True), (True, True)])
def test_omitted_followup_uses_named_binding_before_defaults(browser_boundary, entrypoint, required, restart):
    config, created, commands, router = browser_boundary
    assert navigate(entrypoint, identity='lpg')['success']
    claim = bt._read_browser_identity_binding('named')
    config['require_identity'] = required
    if restart:
        bt._active_sessions.clear()
    assert navigate(entrypoint)['success']
    assert bt._read_browser_identity_binding('named') == claim
    assert bt._active_sessions['named']['browser_identity'] == 'lpg'
    assert all(identity == 'lpg' for _, _, identity in created)
    router.assert_not_called()
    with pytest.raises(RuntimeError, match='already bound'):
        navigate(entrypoint, identity='personal')
    assert [action for _, action in commands].count('open') == 2


@pytest.mark.parametrize('entrypoint', ['direct', 'registered'])
def test_bound_identity_cannot_escape_to_extension_after_config_removed(browser_boundary, entrypoint):
    config, created, commands, router = browser_boundary
    assert navigate(entrypoint, identity='lpg')['success']
    config.clear()
    result = navigate(entrypoint)
    assert result['success'] is False
    assert 'unknown browser identity' in result['error']
    assert len(created) == 1
    router.assert_not_called()


@pytest.mark.parametrize('entrypoint', ['direct', 'registered'])
@pytest.mark.parametrize('configuration', ['default', 'required', 'invalid'])
def test_private_navigation_ignores_unused_identity_configuration(browser_boundary, monkeypatch, entrypoint, configuration):
    config, created, commands, router = browser_boundary
    if configuration == 'required':
        config['require_identity'] = True
    elif configuration == 'invalid':
        config['real_profile_identities'] = 'invalid unused configuration'
    provider = Mock()
    monkeypatch.setattr(cloud, '_get_cloud_provider', lambda: provider)
    monkeypatch.setattr(cloud, '_auto_local_for_private_urls', lambda: True)
    monkeypatch.setattr(cloud, '_is_local_backend', lambda: False)
    assert navigate(entrypoint, task='private', url='http://127.0.0.1:3000/')['success']
    assert created == [('private::local', False, None)]
    assert bt._read_browser_identity_binding('private') is None
    assert bt._read_browser_identity_binding('private::local') is None
    router.assert_not_called()
    provider.create_session.assert_not_called()
    assert commands[0] == ('private::local', 'open')


def test_force_local_session_ignores_identity_and_cdp_but_new_navigation_respects_cdp(browser_boundary, monkeypatch):
    config, created, _, _ = browser_boundary
    config['require_identity'] = True
    monkeypatch.setattr(bt, '_get_cdp_override', Mock(side_effect=AssertionError('sidecar read CDP override')))
    monkeypatch.setattr(cdp, '_get_cdp_override_raw', lambda: 'ws://operator:9222')
    monkeypatch.setattr('hermes_cli.browser_identity.resolve_browser_identity', Mock(side_effect=AssertionError('sidecar resolved identity')))
    info = sessions._get_session_info('private::local', identity='unused')
    assert info['features']['real_profile'] is False
    assert created == [('private::local', False, None)]
    assert bt._read_browser_identity_binding('private::local') is None
    assert bt._navigation_session_key('new-task', 'http://127.0.0.1:3000/') == 'new-task'


@pytest.mark.parametrize('backend', ['cdp_override', 'cloud_provider'])
def test_backend_rejection_before_attachment_leaves_no_durable_identity_claim(browser_boundary, monkeypatch, backend):
    """A named identity refused by the backend configuration must not bind the task: once the
    operator fixes the configuration, any valid alias (not just the refused one) must still work."""
    config, created, _, _ = browser_boundary
    if backend == 'cdp_override':
        monkeypatch.setattr(bt, '_get_cdp_override_raw', lambda: 'ws://operator:9222')
        monkeypatch.setattr(bt, '_get_cdp_override', Mock(side_effect=AssertionError('gate resolved the CDP URL')))
        expected = 'incompatible'
    else:
        monkeypatch.setattr(bt, '_get_cloud_provider', lambda: Mock())
        expected = 'cloud providers cannot use'
    with pytest.raises(RuntimeError, match=expected):
        sessions._get_session_info('rejected', identity='lpg')
    assert created == []
    assert bt._read_browser_identity_binding('rejected') is None
    assert 'rejected' not in bt._active_sessions

    # Operator removes the incompatible backend and picks a different valid alias.
    monkeypatch.setattr(bt, '_get_cdp_override_raw', lambda: None)
    monkeypatch.setattr(bt, '_get_cdp_override', lambda: None)
    monkeypatch.setattr(bt, '_get_cloud_provider', lambda: None)
    info = sessions._get_session_info('rejected', identity='personal')
    assert info['browser_identity'] == 'personal'
    assert bt._read_browser_identity_binding('rejected')[0] == 'personal'
    assert created == [('rejected', True, 'personal')]


def test_backend_rejection_never_unbinds_an_already_bound_task(browser_boundary, monkeypatch):
    """The fix must only skip a NEW claim; a task that already owns its cookie jar keeps it."""
    config, created, _, _ = browser_boundary
    first = sessions._get_session_info('bound', identity='lpg')
    assert first['browser_identity'] == 'lpg'
    claim = bt._read_browser_identity_binding('bound')
    bt._active_sessions.clear()
    monkeypatch.setattr(bt, '_get_cdp_override_raw', lambda: 'ws://operator:9222')
    with pytest.raises(RuntimeError, match='incompatible'):
        sessions._get_session_info('bound')
    assert bt._read_browser_identity_binding('bound') == claim
    monkeypatch.setattr(bt, '_get_cdp_override_raw', lambda: None)
    with pytest.raises(RuntimeError, match='already bound'):
        sessions._get_session_info('bound', identity='personal')
    assert bt._read_browser_identity_binding('bound') == claim


@pytest.mark.parametrize("identity", ["lpg", None])
def test_consent_refusal_leaves_task_free_for_later_identity(browser_boundary, tmp_path, monkeypatch, identity):
    from hermes_cli import config as config_module

    config, _, _, _ = browser_boundary
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps({"browser": {**config, "use_real_profile": False}}))
    monkeypatch.setattr(config_module, "get_config_path", lambda: path)
    monkeypatch.setattr("hermes_cli.browser_identity.read_browser_identity_config", read_browser_identity_config)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "managed"))
    monkeypatch.setattr(bt, "_use_real_profile", cloud._use_real_profile)
    monkeypatch.setattr(bt, "_create_local_session", sessions._create_local_session)
    monkeypatch.setattr(bt, "_cleanup_real_profile_state", lambda: None)
    attach = Mock(return_value=("ws://127.0.0.1:9999", None))
    monkeypatch.setattr(bt, "_real_profile_cdp", attach)
    monkeypatch.setattr(bt, "_resolve_cdp_override", lambda url: url)
    with pytest.raises(RuntimeError, match="use_real_profile"):
        sessions._get_session_info("consent-refused", identity=identity)
    attach.assert_not_called()
    assert bt._read_browser_identity_binding("consent-refused") is None
    assert "consent-refused" not in bt._active_sessions

    path.write_text(json.dumps({"browser": {**config, "use_real_profile": True}}))
    different = "personal" if identity == "lpg" else "lpg"
    info = sessions._get_session_info("consent-refused", identity=different)
    assert info["browser_identity"] == different
    attach.assert_called_once_with(different)
    assert bt._read_browser_identity_binding("consent-refused")[0] == different
