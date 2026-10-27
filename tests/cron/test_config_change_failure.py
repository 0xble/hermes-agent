"""Exercise real script runs that change config and then fail or leave it invalid."""
import pytest


@pytest.mark.parametrize("invalid,exit_code", [(False, 1), (True, 0), (True, 1)])
def test_config_mutating_cron_failure_names_the_job_once(tmp_path, monkeypatch, caplog, invalid, exit_code):
    from cron import jobs, scheduler

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "scripts").mkdir()
    path = tmp_path / "config.yaml"
    path.write_text("display: {tool_progress: false}\n")
    text = "delegation: {model_preset: missing}\n" if invalid else "display: {tool_progress: true}\n"
    script = tmp_path / "scripts" / "edit.py"
    script.write_text(
        "import os, sys\nfrom pathlib import Path\n"
        f"(Path(os.environ['HERMES_HOME']) / 'config.yaml').write_text({text!r})\n"
        f"sys.exit({exit_code})\n"
    )
    job = jobs.create_job(name="quota-burn-fixture", prompt=None, schedule="every 30m",
                          script="edit.py", no_agent=True, deliver="local")
    with caplog.at_level("WARNING"):
        # Use the canonical bookkeeping body in process, with the real run_job/script path.
        assert scheduler._run_one_job_body(job) is True
    saved = jobs.get_job(job["id"])
    assert saved is not None
    assert saved["last_status"] == "error"
    assert "config.yaml changed while cron job 'quota-burn-fixture'" in saved["last_error"]
    warnings = [r.message for r in caplog.records if "config.yaml changed while cron job" in r.message]
    assert len(warnings) == 1
    assert job["id"] in warnings[0]
    assert "config revisions" in warnings[0]
    if invalid:
        assert "config is invalid" in warnings[0]
    else:
        assert "the run failed" in warnings[0]


def test_successful_valid_config_edit_has_no_warning(tmp_path, monkeypatch, caplog):
    from cron import jobs, scheduler

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "scripts").mkdir()
    (tmp_path / "config.yaml").write_text("{}\n")
    (tmp_path / "scripts" / "edit.sh").write_text(
        '#!/bin/sh\nprintf "display: {tool_progress: false}\\n" > "$HERMES_HOME/config.yaml"\n'
    )
    job = jobs.create_job(prompt=None, schedule="every 30m", script="edit.sh", no_agent=True, deliver="local")
    assert scheduler.run_job(job)[0] is True
    assert not any("config.yaml changed while cron job" in r.message for r in caplog.records)
