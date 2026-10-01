"""Forward-only and pre-amendment handover cannot be enabled together."""
import pytest

from gateway.config import GatewayConfig
from gateway.generation import overlap_handover_enabled, forward_only_handover_enabled
from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_cli.config_effective import load_user_config_effective
from hermes_cli.immutable_releases import activate_release, stage_release


@pytest.mark.parametrize('scalar,expected', [('true', True), ('false', False), ('"false"', False)])
def test_forward_flag_survives_runtime_loader_and_defaults_off(tmp_path, scalar, expected):
    path = tmp_path / 'config.yaml'
    path.write_text(f'gateway:\n  forward_only_handover:\n    enabled: {scalar}\n  overlap_handover:\n    enabled: false\n', encoding="utf-8")
    raw = load_user_config_effective(path, fail_closed=True)
    config = GatewayConfig.from_dict(raw)
    assert forward_only_handover_enabled(raw) is expected
    assert overlap_handover_enabled(config) is expected
    assert config.forward_only_handover_enabled is expected
    assert DEFAULT_CONFIG['gateway']['forward_only_handover']['enabled'] is False


@pytest.mark.parametrize('operation', ['config', 'stage', 'activate'])
def test_both_flags_refuse_before_start_or_staging(tmp_path, operation):
    text = 'gateway:\n  forward_only_handover:\n    enabled: true\n  overlap_handover:\n    enabled: true\n'
    path = tmp_path / 'config.yaml'
    path.write_text(text, encoding="utf-8")
    with pytest.raises((ValueError, RuntimeError), match='overlap_handover'):
        if operation == 'config':
            GatewayConfig.from_dict(load_user_config_effective(path, fail_closed=True))
        elif operation == 'stage':
            stage_release(tmp_path / 'source', tmp_path, sha='a' * 40)
        else:
            activate_release(tmp_path, tmp_path / 'candidate')
    assert not (tmp_path / 'releases').exists()
