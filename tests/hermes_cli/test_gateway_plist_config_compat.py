"""Legacy plist rendering tolerates config failures and source installs keep their interpreter."""
import plistlib
from pathlib import Path

import pytest

from hermes_cli import gateway


@pytest.mark.macos_only
@pytest.mark.parametrize('config', ['gateway: [', 'gateway:\n  overlap_handover:\n    enabled: true\n  forward_only_handover:\n    enabled: true\n',
                                   'gateway:\n  forward_only_handover:\n    enabled: true\n'])
def test_service_plist_config_compat_and_source_interpreter(tmp_path, monkeypatch, config):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text(config, encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(gateway, 'get_hermes_home', lambda: home)
    monkeypatch.setattr(gateway, '_stable_service_working_dir', lambda: str(home))
    monkeypatch.setattr(gateway, '_service_venv_dir', lambda: str(Path(gateway.get_python_path()).parent.parent))
    monkeypatch.setattr(gateway, '_build_service_path_dirs', lambda: [])
    monkeypatch.setattr(gateway, '_append_node_dir_for_service', lambda paths: None)
    plist = tmp_path / 'service.plist'
    monkeypatch.setattr(gateway, 'get_launchd_plist_path', lambda: plist)
    rendered = gateway.generate_launchd_plist()
    payload = plistlib.loads(rendered.encode())
    assert gateway.get_python_path() in str(payload['ProgramArguments'])
    plist.write_text(rendered, encoding='utf-8')
    assert gateway.launchd_plist_is_current()
    enabled = config.endswith('enabled: true\n') and 'overlap_handover' not in config
    assert ('HERMES_GENERATION_SCOPE' in payload['EnvironmentVariables']) is enabled
