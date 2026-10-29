"""Recovery diagnostics must remain usable without serving stale runtime settings."""
import pytest

from hermes_cli import config
from hermes_cli.config_errors import ConfigValidationError


@pytest.fixture
def broken_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text("timezone: UTC\n", encoding="utf-8")
    config.load_config()
    path.write_text("timezone: NOT/A_ZONE\n", encoding="utf-8")
    return path


def test_show_reports_invalid_document_without_stale_values_or_crash(broken_config, capsys):
    from hermes_cli.config_show import show_config
    before = broken_config.read_bytes()
    show_config()
    text = capsys.readouterr().out
    assert "config.yaml is invalid" in text
    assert "NOT/A_ZONE" in text
    assert "hermes config edit" in text
    assert "Timezone:     UTC" not in text
    assert broken_config.read_bytes() == before
    with pytest.raises(ConfigValidationError):
        config.load_config()


def test_health_names_the_repairable_validation_error(broken_config):
    issues = config.validate_config_structure()
    assert any(i.severity == "error" and "NOT/A_ZONE" in i.local_detail for i in issues)
    assert all("NOT/A_ZONE" not in i.message for i in issues)
    assert any("hermes config edit" in i.hint for i in issues)


def test_local_warning_retains_validation_detail(broken_config, capsys):
    config.print_config_warnings()
    assert "NOT/A_ZONE" in capsys.readouterr().err


def test_recovery_only_allows_a_fully_valid_repaired_write(broken_config):
    before = broken_config.read_bytes()
    with pytest.raises(SystemExit) as exc:
        config.set_config_value("agent.max_turns", "7")
    assert exc.value.code == 1
    assert broken_config.read_bytes() == before
    config.set_config_value("timezone", "UTC")
    assert config.load_config()["timezone"] == "UTC"
