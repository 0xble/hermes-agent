"""Consent survives revision-bearing raw mappings during old/new-code update handoff."""
import pytest

from hermes_cli import config, update_receipt


@pytest.mark.parametrize("enabled", [False, True, None])
def test_consent_reader_accepts_revision_bearing_snapshot(tmp_path, monkeypatch, enabled):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    text = "{}\n" if enabled is None else f"telemetry:\n  shared_metrics:\n    enabled: {str(enabled).lower()}\n"
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    assert hasattr(config.read_raw_config_readonly(), "source_revision")
    assert update_receipt._collection_enabled_now() is (enabled if enabled is not None else False)


def test_failed_raw_read_is_not_a_consent_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("[broken: yaml", encoding="utf-8")
    assert isinstance(config.read_raw_config_readonly(), config.FailedConfigRead)
    assert update_receipt._collection_enabled_now() is None
