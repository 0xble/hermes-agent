"""Config audit is an interval observation, not the job's outcome or edit attribution."""
import pytest

from cron.config_audit import audit_config_changes


@pytest.mark.parametrize("no_agent", [True, False])
@pytest.mark.parametrize("success", [True, False])
def test_audit_preserves_real_result_after_concurrent_invalid_edit(tmp_path, monkeypatch, caplog, no_agent, success):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.session_cron.current_execution", lambda: object())
    path = tmp_path / "config.yaml"
    path.write_text("{}\n", encoding="utf-8")
    response = "REAL FINAL RESPONSE"
    original_error = None if success else "real script error"

    @audit_config_changes
    def run(_job):
        path.write_text("timezone: NOT/A_ZONE\n", encoding="utf-8")
        return success, "REAL SCRIPT OUTPUT", response, original_error

    with caplog.at_level("WARNING"):
        result = run({"id": "fixture", "name": "observer", "no_agent": no_agent})
    assert result[0] is success
    assert result[1].startswith("REAL SCRIPT OUTPUT")
    assert result[2] == response
    if success:
        assert result[3] is None
    else:
        assert result[3].startswith(original_error)
    warnings = [r.message for r in caplog.records if "config.yaml changed while" in r.message]
    assert len(warnings) == 1
    assert "config is invalid" in result[1]
