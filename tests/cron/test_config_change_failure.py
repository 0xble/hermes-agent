"""Cron config auditing preserves the run result when a concurrent edit is invalid."""
from pathlib import Path

import pytest

from cron.config_audit import audit_config_changes


@pytest.mark.parametrize("success", [True, False])
def test_config_mutating_run_keeps_result_and_warns_once(tmp_path, monkeypatch, caplog, success):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = Path(tmp_path) / "config.yaml"
    path.write_text("display: {tool_progress: false}\n", encoding="utf-8")

    @audit_config_changes
    def run(job):
        path.write_text("delegation: {model_preset: missing}\n", encoding="utf-8")
        return success, "real output", "real response", "real error" if not success else ""

    with caplog.at_level("WARNING"):
        result = run({"id": "fixture", "name": "quota-burn-fixture", "no_agent": True})

    assert result[0] is success
    assert result[1].startswith("real output")
    assert result[2] == "real response"
    assert result[3] == ("real error" if not success else "")
    warnings = [r.message for r in caplog.records if "config.yaml changed while cron job" in r.message]
    assert len(warnings) == 1
    assert "quota-burn-fixture" in warnings[0]
    assert "config is invalid" in warnings[0]


def test_successful_valid_config_edit_has_no_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = Path(tmp_path) / "config.yaml"
    path.write_text("{}\n", encoding="utf-8")

    @audit_config_changes
    def run(job):
        path.write_text("display: {tool_progress: false}\n", encoding="utf-8")
        return True, "output", "response", ""

    assert run({"id": "fixture", "no_agent": True}) == (True, "output", "response", "")
    assert not any("config.yaml changed while cron job" in r.message for r in caplog.records)
