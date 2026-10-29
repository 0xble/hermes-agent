"""Audit admission must reject the same YAML document shapes as the runtime."""
import pytest

from cron.config_audit import _changed_config_warning, _revision
from hermes_cli import config
from hermes_cli.config_errors import ConfigValidationError


@pytest.mark.parametrize("text", ["- invalid-root\n", "false\n", "0\n", "an-invalid-scalar\n", "true\n"])
def test_audit_does_not_normalize_invalid_roots_to_empty_config(tmp_path, monkeypatch, text):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text("{}\n", encoding="utf-8")
    before = _revision(path)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        config.load_config()
    warning = _changed_config_warning({"id": "fixture", "name": "observer"}, path, before, failed=False)
    assert warning is not None and "config is invalid" in warning


@pytest.mark.parametrize("text", ["null\n", "{}\n", "timezone: UTC\n"])
def test_audit_keeps_valid_changed_documents_quiet(tmp_path, monkeypatch, text):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text("timezone: Pacific/Honolulu\n", encoding="utf-8")
    before = _revision(path)
    path.write_text(text, encoding="utf-8")
    assert config.load_config()
    assert _changed_config_warning({"id": "fixture"}, path, before, failed=False) is None
