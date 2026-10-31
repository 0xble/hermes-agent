"""Canary policy is recovery input, not a runtime-config consumer."""
import pytest

from hermes_cli.admission_canary import settings
from hermes_cli.config import load_config
from hermes_cli.config_errors import ConfigValidationError


@pytest.mark.parametrize("unrelated", ["timezone: NOT/A_ZONE", "model: '@missing-preset'"])
def test_canary_settings_can_inspect_invalid_runtime_config(tmp_path, monkeypatch, unrelated):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(unrelated + "\ngateway:\n  guardian:\n    admission_canary:\n      enabled: false\n      window_seconds: 25\n      refusal_threshold: 7\n", encoding="utf-8")
    assert settings(tmp_path) == (False, 25.0, 7)
    with pytest.raises(ConfigValidationError):
        load_config()


def test_canary_still_rejects_invalid_own_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("timezone: NOT/A_ZONE\ngateway:\n  guardian:\n    admission_canary:\n      window_seconds: 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="window_seconds"):
        settings(tmp_path)
