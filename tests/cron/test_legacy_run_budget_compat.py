"""Compatibility coverage for retired cron run-budget records."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock

import pytest


def _install_run_job_stubs(monkeypatch, tmp_path, agent_type):
    import hermes_state
    import run_agent
    from hermes_cli import env_loader, runtime_provider
    import cron.scheduler as scheduler

    fake_db = MagicMock()
    fake_db.get_compression_tip.side_effect = lambda session_id: session_id
    monkeypatch.setenv("HERMES_CRON_SESSION_DB_TIMEOUT", "0")
    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")
    monkeypatch.setattr(scheduler, "_hermes_home", tmp_path)
    monkeypatch.setattr(scheduler, "_resolve_origin", lambda _job: None)
    monkeypatch.setattr(scheduler, "_preflight_job_config", lambda *_a, **_kw: None)
    monkeypatch.setattr(scheduler, "_build_job_prompt", lambda *_a, **_kw: "prompt")
    monkeypatch.setattr(env_loader, "load_hermes_dotenv", lambda **_kw: [])
    monkeypatch.setattr(env_loader, "reset_secret_source_cache", lambda _home=None: None)
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
    monkeypatch.setattr("tools.mcp_tool_discovery.discover_mcp_tools", lambda *_a, **_kw: [])
    monkeypatch.setattr(run_agent, "AIAgent", agent_type)
    return scheduler


def test_legacy_run_budget_is_inert_for_script_and_agent_fire(tmp_path, monkeypatch):
    """A stale field cannot cap pre-agent scripts or be passed to the agent."""
    observed = {}

    class Agent:
        def __init__(self, **kwargs):
            observed["agent_kwargs"] = kwargs
            self.session_id = kwargs["session_id"]

        def run_conversation(self, _prompt, **_kwargs):
            return {"final_response": "done", "messages": []}

        def close(self):
            pass

    scheduler = _install_run_job_stubs(monkeypatch, tmp_path, Agent)

    def run_script(_job, _path, **kwargs):
        observed["script_timeout"] = kwargs.get("timeout_seconds")
        return True, "context"

    monkeypatch.setattr(scheduler, "_run_job_script_with_claim_heartbeat", run_script)

    success, _doc, response, error = scheduler.run_job(
        {
            "id": "legacy-budget",
            "name": "legacy budget",
            "prompt": "work",
            "script": "context.py",
            "run_budget_seconds": 0.001,
        }
    )

    assert success is True
    assert response == "done"
    assert error is None
    assert observed["script_timeout"] is None
    assert "run_budget_seconds" not in observed["agent_kwargs"]


def test_removed_budget_is_absent_from_tool_schema_and_cli_parser():
    from hermes_cli.cron import cron_command
    from hermes_cli.subcommands.cron import build_cron_parser
    from tools.cronjob_tools import CRONJOB_SCHEMA

    assert "run_budget_seconds" not in CRONJOB_SCHEMA["parameters"]["properties"]

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_cron_parser(subparsers, cmd_cron=cron_command)
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["cron", "create", "every 1h", "legacy budget", "--run-budget-seconds", "60"]
        )
