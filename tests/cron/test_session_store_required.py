"""Agent cron fires require persistence before gates can acknowledge work."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cron import jobs, scheduler


@pytest.fixture
def agent_runtime(monkeypatch):
    import run_agent
    import hermes_state_registry
    import hermes_cli.env_loader
    import hermes_cli.runtime_provider

    monkeypatch.setattr(scheduler, "load_config", lambda: {})
    monkeypatch.setattr(scheduler, "_get_session_db_timeout", lambda: 0.2)
    monkeypatch.setattr(scheduler, "_init_cron_mcp_tools", lambda *_: None)
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda *_: False)
    monkeypatch.setattr(hermes_cli.env_loader, "load_hermes_dotenv", lambda **_: None)
    monkeypatch.setattr(
        hermes_cli.env_loader, "reset_secret_source_cache", lambda *_: None
    )
    monkeypatch.setattr(
        hermes_cli.runtime_provider,
        "resolve_runtime_provider",
        lambda **_: {
            "provider": "openrouter",
            "model": "test",
            "api_key": "test-key",
        },
    )
    agent_class = MagicMock()
    agent_class.return_value.run_conversation.return_value = {
        "final_response": "report"
    }
    monkeypatch.setattr(run_agent, "AIAgent", agent_class)
    acquire = MagicMock()
    monkeypatch.setattr(hermes_state_registry, "acquire", acquire)
    return agent_class, acquire


@pytest.fixture
def owner_db(monkeypatch, tmp_path):
    """Run agent jobs inside the owner's cron execution, as the gateway admission does.

    Only the client transport (``run_canonical_job``) is replaced. The owner's store is the
    one handle ``_open_cron_session_db`` accepts, so tests hand it back from ``acquire``.
    """
    from gateway import session_cron

    owner = SimpleNamespace(db=MagicMock(db_path=tmp_path / "state.db"))

    def run(job, *, extra_prompt=None, cancel_event=None, execution_id=None):
        token = session_cron._execution.set((owner, "owner-session", job["id"], execution_id))
        try:
            return scheduler.run_job(job, extra_prompt=extra_prompt, cancel_event=cancel_event,
                                     execution_id=execution_id)
        finally:
            session_cron._execution.reset(token)

    monkeypatch.setattr("cron.scheduler_authority.run_canonical_job", run)
    return owner.db


@pytest.mark.parametrize("recover", [False, True])
def test_agent_requires_handle_after_timeout_and_exception(agent_runtime, owner_db, recover):
    agent_class, acquire = agent_runtime
    handle = owner_db if recover else None
    acquire.side_effect = [TimeoutError(), OSError("locked"), handle]
    success, output, final, error = scheduler.run_job({
        "id": "required-store",
        "prompt": "Generate a report",
        "deliver": "local",
    })
    assert acquire.call_count == 3
    if recover:
        assert success and final == "report" and error is None
        assert agent_class.call_args.kwargs["session_db"] is handle
    else:
        assert not success and final == ""
        assert "session store unavailable; run not started" in error
        assert "session store unavailable; run not started" in output
        agent_class.assert_not_called()


@pytest.mark.parametrize("no_agent", [False, True])
@pytest.mark.parametrize("schedule", ["in 30m", "every 5m"])
def test_store_failure_preserves_one_shot_and_wake_gate(
    agent_runtime, owner_db, monkeypatch, no_agent, schedule
):
    from cron.executions import list_executions
    from cron.incidents import list_incidents
    from cron import monitor

    agent_class, acquire = agent_runtime
    acquire.side_effect = [TimeoutError(), OSError("locked"), None]
    script = MagicMock(return_value=(True, '{"wakeAgent": true}'))
    monkeypatch.setattr(scheduler, "_run_job_script_with_claim_heartbeat", script)
    monitor_source = MagicMock(return_value=(True, "new observation"))
    monkeypatch.setattr(monitor, "_run_monitor_source", monitor_source)
    delivered = MagicMock(return_value=None)
    monkeypatch.setattr(scheduler, "_deliver_result", delivered)
    # The notice target is a fixture: no gateway credentials exist for preflight to find.
    monkeypatch.setattr(scheduler, "_preflight_job_config", lambda *_: None)
    job = jobs.create_job(
        prompt="Generate a report",
        schedule=schedule,
        script="gate.py",
        no_agent=no_agent,
        deliver="telegram:123",
        repeat=1,
    )
    if not no_agent:
        job = jobs.update_job(job["id"], {"monitor_script": "monitor.py"})
    claimed = jobs.claim_job_for_fire(job["id"], manual=True, return_job=True)
    assert scheduler.run_one_job(claimed)
    current = jobs.get_job(job["id"])
    if no_agent:
        acquire.assert_not_called()
        script.assert_called_once()
        agent_class.assert_not_called()
        assert current["last_status"] == "ok"
        assert current["repeat"]["completed"] == 1
    else:
        acquire.assert_called()
        script.assert_not_called()
        monitor_source.assert_not_called()
        assert current["monitor_state"] is None
        agent_class.assert_not_called()
        assert current["last_status"] == "error"
        assert "session store unavailable; run not started" in current["last_error"]
        assert current["repeat"]["completed"] == 0
        assert current["enabled"] and current["state"] == "scheduled"
        assert not current["fire_claim"] and not current.get("run_claim")
        # The failure notice rides the durable queue; the gateway drain performs it.
        assert scheduler.drain_delivery_queue({}, None) == 1
        delivered.assert_called_once()
        assert delivered.call_args.kwargs["for_failure"] is True
        executions = list_executions(job_id=job["id"])
        assert executions[0]["status"] == "failed"
        incident = next(i for i in list_incidents() if i["job_id"] == job["id"])
        assert "session store unavailable; run not started" in incident["error"]
        # Advance past the retry instant: the real due scan must still admit the one-shot.
        from datetime import datetime, timedelta

        retry_at = datetime.fromisoformat(current["next_run_at"])
        monkeypatch.setattr(
            jobs, "_hermes_now", lambda: retry_at + timedelta(seconds=1)
        )
        assert [due["id"] for due in jobs.get_due_jobs()] == [job["id"]]
        acquire.side_effect = None
        handle = owner_db
        acquire.return_value = handle
        retry = jobs.claim_job_for_fire(job["id"], return_job=True)
        assert scheduler.run_one_job(retry)
        script.assert_called_once()
        monitor_source.assert_called_once()
        assert agent_class.call_args.kwargs["session_db"] is handle
        assert jobs.get_job(job["id"])["repeat"]["completed"] == 1
