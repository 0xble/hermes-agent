"""Script execution failures remain failures after a completed agent turn."""

import re
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cron import executions, jobs, scheduler, scheduler_script


@pytest.fixture
def script_run(tmp_path, monkeypatch):
    import run_agent

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    agent = MagicMock()
    agent.run_conversation.return_value = {"final_response": "Normal report", "completed": True}
    monkeypatch.setattr(run_agent, "AIAgent", MagicMock(return_value=agent))
    monkeypatch.setattr(scheduler, "_open_cron_session_db", lambda *_: MagicMock())
    monkeypatch.setattr(scheduler, "_finalize_cron_session", lambda *_, **__: None)
    monkeypatch.setattr(scheduler, "_load_cron_job_config", lambda *_: SimpleNamespace(cfg={}, model="test"))
    monkeypatch.setattr(scheduler, "_resolve_cron_agent_setup", lambda *_: scheduler._CronAgentSetup(model="test"))
    monkeypatch.setattr(scheduler, "_construct_cron_agent", lambda *_, **__: agent)
    monkeypatch.setattr(scheduler, "_reload_dotenv_and_publish_delivery_target", lambda *_: None)
    monkeypatch.setattr(scheduler, "_teardown_cron_agent", lambda *_, **__: None)
    monkeypatch.setattr(scheduler, "_FireAudit", MagicMock())
    queued = MagicMock(return_value={"status": "pending"})
    monkeypatch.setattr("cron.delivery_queue.enqueue", queued)

    def run(source, *, response="Normal report", timeout=30):
        (scripts / "check.py").write_text(source, encoding="utf-8")
        monkeypatch.setattr(scheduler_script, "_get_script_timeout", lambda: timeout)
        agent.run_conversation.side_effect = response if isinstance(response, Exception) else None
        agent.run_conversation.return_value["final_response"] = response if isinstance(response, str) else ""
        job = jobs.create_job(prompt="Explain the script result", schedule="every 5m",
                              script="check.py", interpreter=sys.executable,
                              deliver="telegram", failure_deliver="discord")
        fire = executions.create_execution(job["id"], source="test")["id"]
        job["execution_id"] = fire
        # Exercise run_job inside an admitted owner without opening a session database.
        monkeypatch.setattr("gateway.session_cron.current_execution",
                            lambda: (None, "script-test-session", job["id"], fire))
        assert scheduler.run_one_job(job)
        return jobs.get_job(job["id"]), executions.get_execution(fire), agent, queued

    return run


@pytest.mark.parametrize("source,timeout,evidence", [
    ("import sys\nprint('readiness failed')\nsys.exit(1)\n", 30, "Script exited with code 1"),
    ("import time\ntime.sleep(60)\n", 0, "Script timed out after 0s"),
    ("import sys\nprint('{\"wakeAgent\": false}')\nsys.exit(1)\n", 30, "Script exited with code 1"),
    pytest.param("import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n", 30,
                 "Script exited with code -15", marks=pytest.mark.platforms("posix")),
])
@pytest.mark.parametrize("response", ["Normal report", "NO_REPLY", "[SILENT]", RuntimeError("agent failed")])
def test_failed_script_books_error_and_routes_failure_notice(script_run, source, timeout, evidence, response):
    saved, ledger, agent, queued = script_run(source, timeout=timeout, response=response)
    agent.run_conversation.assert_called_once()
    assert saved["last_status"] == "error"
    assert evidence in saved["last_error"]
    assert ledger["status"] == "failed" and evidence in ledger["error"]
    if "timed out" in evidence:
        assert re.search(r"exit code -?\d+", ledger["error"])
        assert "No model was invoked" not in queued.call_args.args[2]
    assert queued.call_args.kwargs["for_failure"] is True
    assert queued.call_args.args[1]["failure_deliver"] == "discord"
    assert queued.call_args.args[2].strip() not in {"NO_REPLY", "[SILENT]"}
    from cron.incidents import list_incidents
    assert any(item["job_id"] == saved["id"] for item in list_incidents())


@pytest.mark.parametrize("gate", [False, True])
def test_successful_script_preserves_success_and_wake_gate(script_run, gate):
    output = '{"wakeAgent": false}' if gate else "ready"
    saved, ledger, agent, queued = script_run(f"print({output!r})\n")
    assert saved["last_status"] in {"ok", "delivery_queued"}
    assert saved["last_error"] is None
    assert ledger["status"] == "completed" and ledger["error"] is None
    if gate:
        agent.run_conversation.assert_not_called()
        queued.assert_not_called()
    else:
        agent.run_conversation.assert_called_once()
        assert queued.call_args.kwargs["for_failure"] is False
        assert queued.call_args.args[2] == "Normal report"
