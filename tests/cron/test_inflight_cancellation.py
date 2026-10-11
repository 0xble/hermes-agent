"""Operator cancellation must reach the run, not wait for its lease heartbeat."""
import contextvars
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

import cron.jobs as jobs
import cron.scheduler as scheduler
import tools.cronjob_tools  # registers the public tool
from agent.interrupt_scope import track_in_interrupt_scope
from tools.registry import registry


def dispatch(action, **args):
    result = registry.dispatch("cronjob_manage", {"action": action, **args})
    return json.loads(result) if isinstance(result, str) else result


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda job: False)
    monkeypatch.setattr(scheduler, "_RUN_CLAIM_HEARTBEAT_SECONDS", 3600)
    monkeypatch.setattr(tools.cronjob_tools, "_notify_provider_jobs_changed_safe", lambda: None)
    with jobs.use_cron_store(tmp_path):
        yield tmp_path


@pytest.mark.parametrize("action", ["remove", "pause"])
def test_cancel_during_run_interrupts_before_next_side_effect(isolated_store, monkeypatch, action):
    started, proceed, stopped = threading.Event(), threading.Event(), threading.Event()
    side_effects = []
    delivered = Mock(return_value=None)
    monkeypatch.setattr(scheduler, "_deliver_result", delivered)

    class Agent:
        def hard_interrupt(self, message=None, **kwargs):
            stopped.set()

        def interrupt(self, message=None):
            pytest.fail("Cancellation must use the hard-stop path")

        def get_activity_summary(self):
            return {"seconds_since_activity": 0}

        def run_conversation(self, *_args, **_kwargs):
            with track_in_interrupt_scope(self):
                started.set()
                assert proceed.wait(5)
                if not stopped.is_set():
                    side_effects.append("duplicate message")
                return {"final_response": "done", "interrupted": stopped.is_set()}

    def run_job(job, *, cancel_event, **kwargs):
        result = scheduler._run_agent_with_watchdog(
            Agent(), "work", job, job["id"], job["name"], "test-cancel", cancel_event)
        assert cancel_event.is_set()
        return True, "output", result.get("final_response", ""), None

    monkeypatch.setattr(scheduler, "run_job", run_job)
    job = jobs.create_job(prompt="work", schedule="every 5m", deliver="local")
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert claimed
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(contextvars.copy_context().run, scheduler.run_one_job, claimed)
        try:
            assert started.wait(5)
            listed = dispatch("list")["jobs"]
            shown = next(item for item in listed if item["job_id"] == job["id"])
            assert shown["running"] is True and shown["running_since"]
            raw = next(j for j in jobs.list_jobs() if j["id"] == job["id"])
            assert raw["running"] and raw["running_since"]
            assert shown["last_run_at"] is None  # Never-run is NOT idle.
            receipt = dispatch(action, job_id=job["id"])
            assert receipt["success"] is True
            assert receipt["was_running"] is True
            assert receipt["interrupted"] is True
            assert receipt["run_started_at"]
            assert "cannot be undone" in receipt["warning"]
            assert "read back" in receipt["warning"]
            # This is synchronous, before releasing the agent and WITHOUT a heartbeat.
            assert stopped.is_set()
        finally:
            proceed.set()
        future.result(timeout=5)
    assert side_effects == []
    delivered.assert_not_called()
    if action == "pause":
        paused = jobs.get_job(job["id"])
        assert paused is not None
        assert paused["state"] == "paused"
        assert paused["last_error"] == scheduler._CANCELLED_INTERRUPTED
        assert not paused.get("fire_claim")
        shown = next(j for j in dispatch("list", include_disabled=True)["jobs"]
                     if j["job_id"] == job["id"])
        assert not shown["running"] and shown["running_since"] is None
        assert jobs.resume_job(job["id"])
        assert jobs.claim_job_for_fire(job["id"])


@pytest.mark.parametrize("action", ["remove", "pause"])
def test_remote_claim_reports_running_but_not_interrupted(isolated_store, action):
    job = jobs.create_job(prompt="work", schedule="every 5m", deliver="local")
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    started_at = claimed["fire_claim"]["started_at"]
    assert jobs.heartbeat_fire_claim(job["id"], expected_owner=claimed["fire_claim"]["by"])
    shown = next(j for j in dispatch("list")["jobs"] if j["job_id"] == job["id"])
    assert shown["running"] and shown["running_since"] == started_at
    receipt = dispatch(action, job_id=job["id"])
    assert receipt["success"] and receipt["was_running"]
    assert receipt["run_started_at"] == started_at
    assert receipt["interrupted"] is False
    assert "may still perform side effects" in receipt["warning"]
    if action == "pause":
        assert not jobs.heartbeat_fire_claim(job["id"], expected_owner=claimed["fire_claim"]["by"])


@pytest.mark.parametrize("action", ["remove", "pause"])
def test_idle_mutation_reports_no_run(isolated_store, action):
    job = jobs.create_job(prompt="work", schedule="every 5m", deliver="local")
    receipt = dispatch(action, job_id=job["id"])
    assert receipt["success"]
    assert receipt["was_running"] is receipt["interrupted"] is False
    assert receipt["run_started_at"] is None


def test_cancel_scope_latches_before_agent_registration(isolated_store):
    from agent.interrupt_scope import bind_interrupt_scope

    job = jobs.create_job(prompt="work", schedule="every 5m", deliver="local")
    assert scheduler.try_register_running_job(job["id"])
    key = scheduler._inflight_key(job["id"])
    control = scheduler._running_cancel_events[key]
    try:
        receipt = dispatch("remove", job_id=job["id"])
        assert receipt["was_running"] and receipt["interrupted"]
        agent = Mock(spec=["interrupt"])
        with bind_interrupt_scope(control.scope), track_in_interrupt_scope(agent):
            agent.interrupt.assert_called_once()
        assert control.is_set()
    finally:
        scheduler.release_running_job(job["id"])
    assert key not in scheduler._running_cancel_events


@pytest.mark.parametrize("registered", [True, False])
def test_pause_before_start_releases_only_its_own_claim(isolated_store, monkeypatch, registered):
    job = jobs.create_job(prompt="work", schedule="every 5m", deliver="local")
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(claimed, dict)
    runner = Mock()
    monkeypatch.setattr(scheduler, "run_job", runner)
    if registered:
        assert scheduler.try_register_running_job(job["id"])
    try:
        receipt = dispatch("pause", job_id=job["id"])
        assert receipt["was_running"]
        assert receipt["interrupted"] is registered
        assert scheduler.run_one_job(claimed)
    finally:
        if registered:
            scheduler.release_running_job(job["id"])
    runner.assert_not_called()
    paused = jobs.get_job(job["id"])
    assert paused and not paused.get("fire_claim")
    assert paused["last_run_at"] is None
    assert paused["repeat"]["completed"] == 0
    assert not jobs.list_jobs(include_disabled=True)[0]["running"]
    assert jobs.resume_job(job["id"])
    assert jobs.claim_job_for_fire(job["id"])


def test_cancelled_completion_never_clears_replacement_claim(isolated_store):
    job = jobs.create_job(prompt="work", schedule="every 5m", deliver="local")
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    owner = claimed["fire_claim"]["by"]
    jobs.update_job(job["id"], {"fire_claim": {"by": "replacement",
                                             "at": jobs._hermes_now().isoformat()}})
    scheduler._record_fire_ownership_lost(job["id"], owner, "missing-execution")
    assert jobs.get_job(job["id"])["fire_claim"]["by"] == "replacement"
    from cron.scheduler_control import release_unstarted_claim
    release_unstarted_claim(claimed)
    assert jobs.get_job(job["id"])["fire_claim"]["by"] == "replacement"
