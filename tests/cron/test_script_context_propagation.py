"""Cron script callers preserve truthful job identity without ambient routing.

Regression for the narrow marker/propagation follow-up to fork PR #657.
"""

import json
import os
import sys

import pytest


@pytest.mark.parametrize("mode", ["monitor", "pre-run", "prompt", "no-agent", "claimed-no-agent"])
def test_job_script_callers_propagate_cron_context(tmp_path, monkeypatch, mode):
    from cron.scheduler import run_job
    from gateway.session_context import clear_session_vars, set_session_vars

    home = tmp_path / "home"
    scripts = home / "scripts"
    scripts.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SESSION_ID", "stale-launch-session")
    monkeypatch.setenv("HERMES_SESSION_KEY", "stale-launch-route")
    monkeypatch.setenv("HERMES_CRON_SESSION", "stale-launch-marker")
    monkeypatch.setenv("HERMES_CRON_AUTO_DELIVER_CHAT_ID", "stale-launch-chat")
    observed = tmp_path / "observed.json"
    (scripts / "probe.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "names = [name for name in os.environ if name.startswith('HERMES_SESSION_') "
        "or name.startswith('HERMES_CRON_AUTO_DELIVER_') "
        "or name == 'HERMES_CRON_SESSION']\n"
        f"Path({str(observed)!r}).write_text(json.dumps({{name: os.environ[name] for name in names}}))\n"
        "print(json.dumps({'wakeAgent': False}))\n",
        encoding="utf-8",
    )
    (scripts / "gate.py").write_text('print(\'{"wakeAgent": false}\')\n', encoding="utf-8")
    job = {
        "id": "context-probe",
        "name": "Context Probe",
        "prompt": "Inspect the script result",
        "script": "probe.py",
        "interpreter": sys.executable,
        "schedule": {"kind": "interval", "minutes": 30},
    }
    if mode == "monitor":
        job.update(monitor_script="probe.py", script="gate.py")
    if mode in {"no-agent", "claimed-no-agent"}:
        job["no_agent"] = True
    if mode == "claimed-no-agent":
        job.update(schedule={"kind": "once"}, run_claim={"by": "test-owner"})

    # Bound and process-global decoys must both stay out of a job's child.
    tokens = set_session_vars(
        session_id="parent-session", session_key="parent-route", chat_id="parent-chat",
        cron_session="",
    )
    try:
        if mode == "prompt":
            from cron.scheduler_prompt import _build_job_prompt

            assert "Script Output" in _build_job_prompt(job)
        else:
            from types import SimpleNamespace
            from gateway import session_cron
            from hermes_state_registry import acquire, release

            # Exercise the admitted worker using its canonical shared store, not the RPC client.
            db = acquire(home / "state.db")
            token = session_cron.bind_execution(
                SimpleNamespace(db=db), "cron_context-probe", job["id"], None)
            try:
                success, _doc, _response, error = run_job(job)
                assert success, error
            finally:
                session_cron._execution.reset(token)
                release(db)
        assert json.loads(observed.read_text(encoding="utf-8")) == {
            "HERMES_SESSION_ID": "cron_context-probe",
            "HERMES_SESSION_PROFILE": "default",
            "HERMES_CRON_SESSION": "1",
        }
        assert os.environ["HERMES_CRON_SESSION"] == "stale-launch-marker"
        assert os.environ["HERMES_SESSION_ID"] == "stale-launch-session"
    finally:
        clear_session_vars(tokens)
