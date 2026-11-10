"""Agent-declared failures expose meaningful producer metadata, not Markdown fences.

The audit fixture preserves incident 339cad_5d5eea8ff844's response except private URLs.
"""

from pathlib import Path

import pytest

from cron import incidents, jobs, scheduler


_AUDIT_RESPONSE = (Path(__file__).parent / "fixtures" / "agent_failure_audit.md").read_text()


@pytest.mark.parametrize(("response", "summary", "failure_type"), [
    (_AUDIT_RESPONSE, "Audit        24 open, 0 new", "agent"),
    ("[CRON_FAILURE]\n\n```text\n\nReadTimeout: export stalled\n```\n"
     "Authentication was checked successfully.", "ReadTimeout: export stalled", "timeout"),
    ("[CRON_FAILURE]\n\n~~~text\nHTTP 401 Unauthorized\n~~~", "HTTP 401 Unauthorized", "auth"),
    ("[CRON_FAILURE]\n\n```text\n```", "Cron agent reported failure.", "agent"),
    ("[CRON_FAILURE]\nChild failed.\nAuthentication failed in unrelated history.", "Child failed.", "agent"),
])
def test_declared_failure_persists_meaningful_summary_and_type(tmp_path, monkeypatch, response, summary, failure_type):
    with jobs.use_cron_store(tmp_path / "cron"):
        job = jobs.create_job(prompt="report", schedule="every 1h", deliver="local")
        monkeypatch.setattr(scheduler, "run_job", lambda *_a, **_k: (True, response, response, None))

        assert scheduler.run_one_job(job)

        saved = jobs.get_job(job["id"])
        incident, = incidents.list_incidents()
        assert saved is not None
        assert saved["last_status"] == "error"
        assert saved["last_error"].splitlines()[0] == summary
        assert incident["error"].splitlines()[0] == summary
        assert incident["failure_type"] == failure_type
        assert response in Path(incident["output_file"]).read_text()


@pytest.mark.parametrize("response", [
    "Report quotes [CRON_FAILURE] but succeeded.",
    "[CRON_FAILURE] inline explanation",
    "\n[CRON_FAILURE]\nnot the first line",
    "```text\n[CRON_FAILURE]\nquoted marker\n```",
])
def test_only_standalone_first_line_marker_changes_run_outcome(tmp_path, monkeypatch, response):
    with jobs.use_cron_store(tmp_path / "cron"):
        job = jobs.create_job(prompt="report", schedule="every 1h", deliver="local")
        monkeypatch.setattr(scheduler, "run_job", lambda *_a, **_k: (True, response, response, None))

        assert scheduler.run_one_job(job)

        assert jobs.get_job(job["id"])["last_status"] == "ok"
        assert incidents.list_incidents() == []
