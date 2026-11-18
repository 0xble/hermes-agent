"""Script execution failures remain failures after a completed agent turn."""

import asyncio
import json
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
    monkeypatch.setattr("cron.detached_authority.detached_assignment", lambda *_: None)

    def canonical(job, *, execution_id, extra_prompt=None, cancel_event=None):
        from gateway.session_contract import SessionRef
        from gateway.session_cron import execute

        authority = SimpleNamespace(
            db=SimpleNamespace(db_path=str(tmp_path / "owner.sqlite")), pending_results={},
            sessions={"script-test-session": SimpleNamespace(source=SimpleNamespace(user_id="cron-owner"))})
        row = {"admission_id": "script-test", "request_id": execution_id,
               "principal_id": "cron-owner", "payload": {"text": extra_prompt or ""}}
        policy = SimpleNamespace(request_json=json.dumps({
            "cron_job": job, "request_id": execution_id, "extra_prompt": extra_prompt}))
        try:
            asyncio.run(execute(authority, SessionRef("test", "script-test-session"), row, policy))
        except RuntimeError:
            if "script-test" not in authority.pending_results:
                raise
        saved = json.loads(json.dumps(authority.pending_results["script-test"]["result"]))
        job.update(saved["cron_job_flags"])
        return tuple(saved["cron_result"])

    monkeypatch.setattr("cron.scheduler_authority.run_canonical_job", canonical)

    def run(source, *, response="Normal report", timeout=30, no_agent=False):
        (scripts / "check.py").write_text(source, encoding="utf-8")
        monkeypatch.setattr(scheduler_script, "_get_script_timeout", lambda: timeout)
        agent.run_conversation.side_effect = response if isinstance(response, Exception) else None
        agent.run_conversation.return_value["final_response"] = response if isinstance(response, str) else ""
        job = jobs.create_job(prompt="Explain the script result", schedule="every 5m",
                              script="check.py", interpreter=sys.executable,
                              deliver="telegram", failure_deliver="discord", no_agent=no_agent)
        fire = executions.create_execution(job["id"], source="test")["id"]
        job["execution_id"] = fire
        assert scheduler.run_one_job(job)
        return jobs.get_job(job["id"]), executions.get_execution(fire), agent, queued

    return run


@pytest.mark.parametrize("stderr", [
    "HTTP 429 Too Many Requests", "Connection timed out", "ReadTimeout", "HTTP 401 Unauthorized",
])
@pytest.mark.parametrize("boundary,no_agent", [
    ("delivery", False), ("incident", False), ("incident", True), ("signature", False), ("signature", True),
])
def test_script_stderr_keeps_script_attribution(script_run, stderr, no_agent, boundary):
    from cron.incidents import get_incident, list_incidents, upsert_incident

    saved, ledger, _, queued = script_run(
        f"import sys\nprint({stderr!r}, file=sys.stderr)\nsys.exit(7)\n", no_agent=no_agent)
    incident = next(item for item in list_incidents() if item["job_id"] == saved["id"])
    if boundary == "delivery":
        notice = queued.call_args.args[2]
        assert "check.py" in notice and "code 7" in notice
        assert "AI model service" not in notice and "backup provider" not in notice
        assert ("No model was invoked" in notice) is no_agent
    elif boundary == "incident":
        assert incident["failure_type"] == "script"
        assert stderr in incident["error"]
    else:
        # The same text from a model failure must not inherit the script incident's ack/signature.
        other_id, is_new = upsert_incident(saved["id"], ledger["error"], failure_type="agent")
        assert is_new and other_id != incident["id"]
        assert get_incident(other_id)["failure_type"] == "agent"


@pytest.mark.parametrize("explanation", [
    "The readiness check failed because the required file is missing.", "", "NO_REPLY", "[SILENT]",
])
def test_script_failure_delivers_notice_then_agent_explanation(script_run, explanation):
    _, _, _, queued = script_run("raise SystemExit(1)\n", response=explanation)
    notice = queued.call_args.args[2]
    assert "check.py" in notice and "code 1" in notice
    if explanation and explanation not in {"NO_REPLY", "[SILENT]"}:
        assert notice.endswith("\n\n" + explanation)
    else:
        assert "NO_REPLY" not in notice and "[SILENT]" not in notice


@pytest.mark.parametrize("trigger", ["timeout", "cancel", "completion"])
def test_script_failure_with_unknown_exit_code(tmp_path, monkeypatch, trigger):
    import threading

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "check.py").write_text("pass\n", encoding="utf-8")
    child = MagicMock(returncode=None)
    child.communicate.return_value = ("", "")
    monkeypatch.setattr(scheduler_script.subprocess, "Popen", MagicMock(return_value=child))
    monkeypatch.setattr(scheduler_script, "_terminate_cron_script_tree", MagicMock())
    monkeypatch.setattr(scheduler_script, "_sweep_detached_script_orphans", MagicMock())
    monkeypatch.setattr(scheduler_script, "_drain_script_pipes", MagicMock())
    monkeypatch.setattr(scheduler_script, "_get_script_timeout", lambda: 0 if trigger == "timeout" else 30)
    cancel = threading.Event()
    if trigger == "cancel":
        cancel.set()
    success, error = scheduler_script._run_job_script("check.py", cancel_event=cancel)
    assert not success
    assert "exit code unknown" in error
    assert "None" not in error


def test_script_timeout_notice_explains_no_agent_mode():
    error = "Script timed out after 30s: check.py (exit code -15)"
    script_only = scheduler._summarize_cron_failure_for_delivery({"id": "check", "no_agent": True}, error)
    agent_backed = scheduler._summarize_cron_failure_for_delivery({"id": "check"}, error)
    assert "No model was invoked" in script_only
    assert "No model was invoked" not in agent_backed


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
