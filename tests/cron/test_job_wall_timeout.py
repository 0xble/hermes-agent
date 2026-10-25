"""Operator-selected job deadlines reuse detached-worker ownership and teardown."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from cron import executions, jobs
from cron.scheduler_detached_worker import hard_wall_timeout_seconds
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture
def profile(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    token = set_hermes_home_override(home)
    try:
        yield home
    finally:
        reset_hermes_home_override(token)


def test_operator_deadline_round_trip_preserves_other_jobs_and_model_dispatch(profile, monkeypatch):
    from hermes_cli import cron as cli
    from hermes_cli.subcommands.cron import build_cron_parser
    from tools import cronjob_tools  # noqa: F401 — register the real model-facing handler
    from tools.registry import registry

    monkeypatch.setenv("HERMES_CRON_SCHEDULER", "builtin")
    monkeypatch.setattr(cli, "_warn_if_gateway_not_running", lambda: None)
    # Read the real profile config, not a patched deadline resolver.
    (profile / "config.yaml").write_text("cron:\n  hard_wall_timeout_seconds: 30\n", encoding="utf-8")
    parser = argparse.ArgumentParser()
    build_cron_parser(parser.add_subparsers(dest="command"), cmd_cron=lambda args: None)
    args = parser.parse_args(["cron", "create", "every 1h", "bounded check", "--hard-wall-timeout", "12"])
    assert cli.cron_create(args) == 0
    job = jobs.list_jobs()[0]
    assert hard_wall_timeout_seconds(job) == 12

    # The model can create/update a job without this operator-only parameter.
    assert "hard_wall_timeout_seconds" not in registry.get_schema("cronjob_manage")["parameters"]["properties"]
    result = json.loads(registry.dispatch("cronjob_manage", {
        "action": "create", "prompt": "long-running work", "schedule": "every 1h"}))
    assert result["success"], result
    sibling = jobs.get_job(result["job_id"])
    assert "hard_wall_timeout_seconds" not in sibling
    assert hard_wall_timeout_seconds(sibling) == 30
    result = json.loads(registry.dispatch("cronjob_manage", {
        "action": "update", "job_id": sibling["id"], "prompt": "updated work"}))
    assert result["success"], result
    assert hard_wall_timeout_seconds(jobs.get_job(sibling["id"])) == 30

    edit = parser.parse_args(["cron", "edit", job["id"], "--hard-wall-timeout", "6"])
    assert cli.cron_edit(edit) == 0
    stored = jobs.get_job(job["id"])
    assert hard_wall_timeout_seconds(stored) == 6
    for value in (-1, float("nan"), float("inf"), True, "invalid", 10 ** 400):
        with pytest.raises(ValueError, match="hard_wall_timeout_seconds"):
            jobs.update_job(job["id"], {"hard_wall_timeout_seconds": value})
        assert jobs.get_job(job["id"]) == stored
        with pytest.raises(ValueError, match="hard_wall_timeout_seconds"):
            jobs.create_job("invalid deadline", "every 1h", hard_wall_timeout_seconds=value)
        assert hard_wall_timeout_seconds({"hard_wall_timeout_seconds": value}) == 30
    clear = parser.parse_args(["cron", "edit", job["id"], "--hard-wall-timeout", "0"])
    assert cli.cron_edit(clear) == 0
    assert not jobs.get_job(job["id"]).get("hard_wall_timeout_seconds")
    assert hard_wall_timeout_seconds(jobs.get_job(job["id"])) == 30
    assert hard_wall_timeout_seconds(jobs.get_job(sibling["id"])) == 30


@pytest.mark.platforms("macos")
def test_detached_script_uses_job_deadline_and_kills_owned_child(profile, tmp_path):
    """Real payload adoption, script subprocess, watchdog, SQLite failure and teardown."""
    child_pid = tmp_path / "child.pid"
    script = profile / "scripts" / "slow.py"
    script.parent.mkdir()
    script.write_text("import os,time\nfrom pathlib import Path\n"
                      f"Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
                      "time.sleep(30)\nprint('late success')\n", encoding="utf-8")
    job = jobs.create_job("bounded script", "every 1h", script=str(script), no_agent=True,
                          deliver="local")
    # Raw payload also proves the runtime honors persisted overrides independently
    # of create-time validation (and reaches the old worker on the base revision).
    job["hard_wall_timeout_seconds"] = 3
    run = executions.create_execution(job["id"], source="builtin")
    assert executions.mark_execution_handoff_pending(run["id"])
    job["execution_id"] = run["id"]
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"job": job, "profile_home": str(profile)}), encoding="utf-8")
    ack = tmp_path / "ack.json"
    code = "from pathlib import Path; from cron.scheduler import _run_external_worker_payload; import sys; sys.exit(0 if _run_external_worker_payload(Path(sys.argv[1]),Path(sys.argv[2])) else 1)"
    proc = subprocess.Popen([sys.executable, "-c", code, str(payload), str(ack)],
                            cwd=Path(__file__).resolve().parents[2],
                            env={**os.environ, "HERMES_HOME": str(profile)}, start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate(timeout=12)
    except subprocess.TimeoutExpired:
        # Only processes created by this test are eligible for fallback cleanup.
        import psutil
        for child in psutil.Process(proc.pid).children(recursive=True):
            child.kill()
        proc.kill()
        out, err = proc.communicate()
        pytest.fail(f"job deadline did not terminate worker: {out}\n{err}")
    assert proc.returncode == 124, (out, err)
    row = executions.get_execution(run["id"])
    assert row["status"] == "failed"
    assert "hard wall-clock timeout (3s)" in row["error"]
    assert ack.exists() and child_pid.exists()
    import psutil
    assert not psutil.pid_exists(int(child_pid.read_text(encoding="utf-8")))
    assert not list(jobs._job_output_dir(job["id"]).glob("*.md"))
