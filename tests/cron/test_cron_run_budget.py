"""Per-job total wall-clock budget contracts for cron execution."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
from unittest.mock import MagicMock

import pytest

from cron.jobs import create_job, load_jobs, update_job


@pytest.fixture()
def cron_store(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")


def _create(**kwargs):
    return create_job(prompt="bounded work", schedule="every 1h", **kwargs)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        (0, None),
        ("0", None),
        (12, 12.0),
        ("12.5", 12.5),
    ],
)
def test_normalize_run_budget_seconds(raw, expected):
    from cron.jobs import _normalize_run_budget_seconds

    assert _normalize_run_budget_seconds(raw) == expected


@pytest.mark.parametrize("raw", [-1, "wat", True, float("nan")])
def test_normalize_run_budget_seconds_rejects_invalid_values(raw):
    from cron.jobs import _normalize_run_budget_seconds

    with pytest.raises(ValueError, match="run_budget_seconds"):
        _normalize_run_budget_seconds(raw)


def test_create_persists_positive_run_budget(cron_store):
    job = _create(run_budget_seconds="12.5")

    assert job["run_budget_seconds"] == 12.5
    assert load_jobs()[0]["run_budget_seconds"] == 12.5


def test_create_without_run_budget_preserves_legacy_shape(cron_store):
    job = _create()

    assert "run_budget_seconds" not in job
    assert "run_budget_seconds" not in load_jobs()[0]


def test_update_absent_preserves_existing_run_budget(cron_store):
    job = _create(run_budget_seconds=30)

    updated = update_job(job["id"], {"name": "renamed"})

    assert updated["run_budget_seconds"] == 30.0


@pytest.mark.parametrize("clearing_value", [None, "", 0, "0"])
def test_update_empty_or_zero_clears_run_budget(cron_store, clearing_value):
    job = _create(run_budget_seconds=30)

    updated = update_job(job["id"], {"run_budget_seconds": clearing_value})

    assert "run_budget_seconds" not in updated
    assert "run_budget_seconds" not in load_jobs()[0]


def test_invalid_update_leaves_stored_run_budget_unchanged(cron_store):
    job = _create(run_budget_seconds=30)

    with pytest.raises(ValueError, match="run_budget_seconds"):
        update_job(job["id"], {"run_budget_seconds": -1})

    assert load_jobs()[0]["run_budget_seconds"] == 30.0


def test_tool_format_includes_only_configured_run_budget():
    from tools.cronjob_tools import _format_job

    assert _format_job({"id": "a", "run_budget_seconds": 25})["run_budget_seconds"] == 25
    assert "run_budget_seconds" not in _format_job({"id": "b"})


def test_model_tool_schema_exposes_total_run_budget():
    from tools.cronjob_tools import CRONJOB_SCHEMA

    field = CRONJOB_SCHEMA["parameters"]["properties"]["run_budget_seconds"]
    assert field["type"] == "number"
    assert field["minimum"] == 0
    assert "total wall-clock" in field["description"]
    assert "zero" in field["description"].lower()


def test_model_tool_create_update_and_clear_run_budget(cron_store):
    import tools.cronjob_tools as cronjob_tools

    handler = cronjob_tools.registry._tools["cronjob"].handler
    created = json.loads(
        handler(
            {
                "action": "create",
                "prompt": "daily bounded digest",
                "schedule": "every 1h",
                "run_budget_seconds": 20,
            }
        )
    )
    assert created["success"] is True
    assert load_jobs()[0]["run_budget_seconds"] == 20.0

    updated = json.loads(
        handler(
            {
                "action": "update",
                "job_id": created["job_id"],
                "run_budget_seconds": 0,
            }
        )
    )
    assert updated["success"] is True
    assert "run_budget_seconds" not in load_jobs()[0]


def _cron_parser():
    from hermes_cli.cron import cron_command
    from hermes_cli.subcommands.cron import build_cron_parser

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_cron_parser(subparsers, cmd_cron=cron_command)
    return parser


def test_cli_create_list_edit_and_clear_run_budget(cron_store, capsys):
    from hermes_cli.cron import cron_command

    parser = _cron_parser()
    create_args = parser.parse_args(
        [
            "cron",
            "create",
            "every 1h",
            "bounded digest",
            "--run-budget-seconds",
            "25",
        ]
    )
    assert cron_command(create_args) == 0
    job = load_jobs()[0]
    assert job["run_budget_seconds"] == 25.0

    assert cron_command(parser.parse_args(["cron", "list", "--all"])) == 0
    assert "Run budget: 25s total wall clock" in capsys.readouterr().out

    edit_args = parser.parse_args(
        [
            "cron",
            "edit",
            job["id"],
            "--run-budget-seconds",
            "0",
        ]
    )
    assert cron_command(edit_args) == 0
    assert "run_budget_seconds" not in load_jobs()[0]


def test_script_runner_caps_its_timeout_to_remaining_total_budget(
    tmp_path, monkeypatch
):
    import cron.scheduler as scheduler

    home = tmp_path / ".hermes"
    scripts = home / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "slow.py").write_text("pass\n", encoding="utf-8")
    communicate_timeouts = []

    class NeverFinishes:
        returncode = None
        stdout = None
        stderr = None
        pid = 123

        def poll(self):
            return None

        def communicate(self, timeout=None):
            communicate_timeouts.append(timeout)
            raise subprocess.TimeoutExpired(cmd="slow.py", timeout=timeout)

    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: home)
    monkeypatch.setattr(scheduler, "_get_script_timeout", lambda: 30.0)
    monkeypatch.setattr(scheduler.subprocess, "Popen", lambda *_a, **_kw: NeverFinishes())
    monkeypatch.setattr(scheduler, "_terminate_cron_script_process", lambda _proc: None)
    monkeypatch.setattr(scheduler, "_drain_script_pipes", lambda _proc: None)
    ticks = iter([0.0, 0.0, 7.0])
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: next(ticks))

    ok, error = scheduler._run_job_script("slow.py", timeout_seconds=7.0)

    assert ok is False
    assert communicate_timeouts == [0.1]
    assert "timed out after 7s" in error


def _install_run_job_stubs(monkeypatch, tmp_path, agent_type):
    import cron.scheduler as scheduler
    import hermes_state
    import run_agent
    from hermes_cli import env_loader, runtime_provider
    from tools import mcp_tool

    fake_db = MagicMock()
    fake_db.get_compression_tip.side_effect = lambda session_id: session_id
    monkeypatch.setenv("HERMES_CRON_SESSION_DB_TIMEOUT", "0")
    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")
    monkeypatch.setattr(scheduler, "_hermes_home", tmp_path)
    monkeypatch.setattr(scheduler, "_resolve_origin", lambda _job: None)
    monkeypatch.setattr(scheduler, "_preflight_job_config", lambda *_a, **_kw: None)
    monkeypatch.setattr(scheduler, "_build_job_prompt", lambda *_a, **_kw: "prompt")
    monkeypatch.setattr(env_loader, "load_hermes_dotenv", lambda **_kw: [])
    monkeypatch.setattr(env_loader, "reset_secret_source_cache", lambda: None)
    monkeypatch.setattr(hermes_state, "SessionDB", lambda: fake_db)
    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        lambda **_kw: {
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
            "provider": "openrouter",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr(mcp_tool, "discover_mcp_tools", lambda: [])
    monkeypatch.setattr(run_agent, "AIAgent", agent_type)
    return scheduler, fake_db


def test_run_job_passes_remaining_budget_to_script_and_agent(tmp_path, monkeypatch):
    observed = {}

    class Agent:
        def __init__(self, **kwargs):
            observed["agent_budget"] = kwargs.get("run_budget_seconds")
            self.session_id = kwargs["session_id"]

        def run_conversation(self, _prompt, **_kw):
            return {"final_response": "done", "messages": []}

        def close(self):
            pass

    scheduler, _ = _install_run_job_stubs(monkeypatch, tmp_path, Agent)
    clock = {"calls": 0}

    def monotonic():
        clock["calls"] += 1
        return 100.0 if clock["calls"] == 1 else 103.0

    def run_script(_job, _path, **kwargs):
        observed["script_budget"] = kwargs.get("timeout_seconds")
        return True, "context"

    monkeypatch.setattr(scheduler.time, "monotonic", monotonic)
    monkeypatch.setattr(scheduler, "_run_job_script_with_claim_heartbeat", run_script)

    success, _doc, response, error = scheduler.run_job(
        {
            "id": "remaining-budget",
            "name": "remaining budget",
            "prompt": "work",
            "script": "context.py",
            "run_budget_seconds": 10,
        }
    )

    assert success is True
    assert error is None
    assert response == "done"
    assert observed["script_budget"] == pytest.approx(7.0)
    assert observed["agent_budget"] == pytest.approx(7.0)


def test_continuously_active_agent_still_exhausts_total_budget(tmp_path, monkeypatch):
    class ActiveAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs["session_id"]

        def run_conversation(self, _prompt):
            raise AssertionError("fake future does not execute the worker")

        def get_activity_summary(self):
            return {"seconds_since_activity": 0.0, "last_activity_desc": "active"}

        def close(self):
            pass

    scheduler, _ = _install_run_job_stubs(monkeypatch, tmp_path, ActiveAgent)
    now = {"value": 0.0}

    class NeverDone:
        def done(self):
            return False

        def result(self):
            raise AssertionError("an unfinished future has no result")

    future = NeverDone()

    class Pool:
        def submit(self, *_a, **_kw):
            return future

        def shutdown(self, **_kw):
            pass

    def wait(_futures, timeout=None):
        assert timeout <= 5.0
        now["value"] += 3.0
        return set(), set()

    interrupts = []
    teardowns = []
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(scheduler.concurrent.futures, "ThreadPoolExecutor", lambda **_kw: Pool())
    monkeypatch.setattr(scheduler.concurrent.futures, "wait", wait)
    monkeypatch.setattr(
        scheduler,
        "request_hard_interrupt",
        lambda agent, reason: interrupts.append((agent, reason)),
    )
    monkeypatch.setattr(
        scheduler,
        "_teardown_cron_agent",
        lambda agent, job_id, **kwargs: teardowns.append((agent, job_id, kwargs)),
    )

    deferred = []
    success, doc, response, error = scheduler.run_job(
        {
            "id": "always-active",
            "name": "always active",
            "prompt": "keep working",
            "run_budget_seconds": 5,
        },
        defer_agent_teardown=deferred,
    )

    assert success is False
    assert response == ""
    assert "total execution budget exhausted" in error.lower()
    assert "total execution budget exhausted" in doc.lower()
    assert interrupts and "total execution budget" in interrupts[0][1].lower()
    assert len(teardowns) == 1
    assert teardowns[0][1] == "always-active"
    assert deferred == []


def test_completed_agent_result_wins_at_total_budget_edge(tmp_path, monkeypatch):
    class Agent:
        def __init__(self, **kwargs):
            self.session_id = kwargs["session_id"]

        def run_conversation(self, _prompt):
            raise AssertionError("fake future supplies the completed result")

        def close(self):
            pass

    scheduler, _ = _install_run_job_stubs(monkeypatch, tmp_path, Agent)
    now = {"value": 0.0}
    completed = concurrent.futures.Future()
    completed.set_result({"final_response": "finished at edge", "messages": []})

    pool_count = {"value": 0}

    class Pool:
        def __init__(self):
            pool_count["value"] += 1
            self.kind = pool_count["value"]

        def submit(self, fn, *args, **kwargs):
            if self.kind == 1:
                session_future = concurrent.futures.Future()
                session_future.set_result(fn(*args, **kwargs))
                return session_future
            now["value"] = 5.0
            return completed

        def shutdown(self, **_kw):
            pass

    monkeypatch.setattr(scheduler.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(
        scheduler.concurrent.futures, "ThreadPoolExecutor", lambda **_kw: Pool()
    )

    success, _doc, response, error = scheduler.run_job(
        {
            "id": "edge-complete",
            "name": "edge complete",
            "prompt": "finish",
            "run_budget_seconds": 5,
        }
    )

    assert success is True
    assert response == "finished at edge"
    assert error is None


def test_no_agent_script_obeys_total_budget_and_is_not_provider_classified(
    tmp_path, monkeypatch
):
    import cron.scheduler as scheduler

    now = {"value": 0.0}
    observed = {}

    def run_script(_job, _path, **kwargs):
        observed["timeout_seconds"] = kwargs.get("timeout_seconds")
        now["value"] = 5.0
        return False, "Script timed out after 5s"

    monkeypatch.setattr(scheduler.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(scheduler, "_run_job_script_with_claim_heartbeat", run_script)
    monkeypatch.setattr("hermes_cli.env_loader.load_hermes_dotenv", lambda **_kw: [])
    job = {
        "id": "no-agent-budget",
        "name": "no agent budget",
        "no_agent": True,
        "script": "slow.py",
        "run_budget_seconds": 5,
    }

    success, doc, _response, error = scheduler.run_job(job)
    summary = scheduler._summarize_cron_failure_for_delivery(job, error)

    assert success is False
    assert observed["timeout_seconds"] == pytest.approx(5.0)
    assert "total execution budget exhausted" in error.lower()
    assert "total execution budget exhausted" in doc.lower()
    assert "provider" not in summary.lower()
    assert "fallback" not in summary.lower()
    assert "total execution budget" in summary.lower()


def test_absent_total_budget_is_dormant_for_no_agent_script(tmp_path, monkeypatch):
    import cron.scheduler as scheduler

    observed = {}

    def run_script(_job, _path, **kwargs):
        observed["timeout_seconds"] = kwargs.get("timeout_seconds")
        return True, "ok"

    monkeypatch.setattr(scheduler, "_run_job_script_with_claim_heartbeat", run_script)
    monkeypatch.setattr("hermes_cli.env_loader.load_hermes_dotenv", lambda **_kw: [])

    success, _doc, response, error = scheduler.run_job(
        {
            "id": "legacy-no-budget",
            "name": "legacy",
            "no_agent": True,
            "script": "probe.py",
        }
    )

    assert success is True
    assert response == "ok"
    assert error is None
    assert observed["timeout_seconds"] is None
