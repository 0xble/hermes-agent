"""Plist config failures stay compatible, and source launchers preserve their native owner."""
import json
import plistlib
import sys
from pathlib import Path

import pytest

from hermes_cli import gateway


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("source_kind", ["managed", "external"])
@pytest.mark.parametrize('config', ['gateway: [', 'gateway:\n  overlap_handover:\n    enabled: true\n  forward_only_handover:\n    enabled: true\n',
                                   'gateway:\n  forward_only_handover:\n    enabled: true\n'])
def test_service_plist_config_compat_and_source_interpreter(tmp_path, monkeypatch, config, source_kind):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text(config, encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(gateway, 'get_hermes_home', lambda: home)
    monkeypatch.setattr(gateway, '_stable_service_working_dir', lambda: str(home))
    source = tmp_path / 'source'
    source.mkdir()
    monkeypatch.setattr(gateway, 'PROJECT_ROOT', source)
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    monkeypatch.setenv('HERMES_RUNTIME_DIR', str(runtime))
    if source_kind == 'managed':
        interpreter = runtime / 'python-fixture' / 'bin' / 'python3'
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        (runtime / 'facts.json').write_text(json.dumps({
            'packages': {'python': {'entry': 'python-fixture'}}}), encoding='utf-8')
    else:
        interpreter = Path(sys.executable)
    assert gateway.get_python_path() == str(interpreter)
    monkeypatch.setattr(gateway, '_build_service_path_dirs', lambda: [])
    monkeypatch.setattr(gateway, '_append_node_dir_for_service', lambda paths: None)
    plist = tmp_path / 'service.plist'
    monkeypatch.setattr(gateway, 'get_launchd_plist_path', lambda: plist)
    rendered = gateway.generate_launchd_plist()
    payload = plistlib.loads(rendered.encode())
    arguments = str(payload['ProgramArguments'])
    if source_kind == 'managed':
        # PM source definitions persist the install launcher, which resolves
        # the current tool at each start instead of pinning a disposable tool.
        assert str(source / '.hermes' / 'bin' / 'hermes') in arguments
        assert str(interpreter) not in arguments
    else:
        assert str(interpreter) in arguments
    assert '--external-supervisor' in arguments and '--replace' not in arguments
    assert 'hermes_cli.stderr_timestamp' in arguments
    plist.write_text(rendered, encoding='utf-8')
    assert gateway.launchd_plist_is_current()
    enabled = config.endswith('enabled: true\n') and 'overlap_handover' not in config
    assert ('HERMES_GENERATION_SCOPE' in payload['EnvironmentVariables']) is enabled
