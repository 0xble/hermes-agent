"""Operator cancellation must reach the run, not wait for its lease heartbeat."""
import contextvars
import json
import threading
from datetime import timedelta
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


@pytest.mark.parametrize("action", ["remove", "pause", "disable"])
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
    job = jobs.create_job(prompt="work", schedule="every 5m", deliver="local", repeat=3)
    jobs.update_job(job["id"], {"failure_streak": 2})
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
            if action == "disable":
                updated = jobs.update_job(job["id"], {"enabled": False})
                assert updated is not None and updated["enabled"] is False
            else:
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
    if action in {"pause", "disable"}:
        paused = jobs.get_job(job["id"])
        assert paused is not None
        assert not paused["enabled"]
        if action == "pause":
            assert paused["state"] == "paused"
        assert paused["last_error"] == scheduler._CANCELLED_INTERRUPTED
        assert not paused.get("fire_claim")
        assert paused["failure_streak"] == 2 and paused["repeat"]["completed"] == 0
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
        assert not receipt["was_running"] and not receipt["interrupted"]
        assert "warning" not in receipt
        agent = Mock(spec=["interrupt"])
        with bind_interrupt_scope(control.scope), track_in_interrupt_scope(agent):
            agent.interrupt.assert_called_once()
        assert control.is_set()
    finally:
        scheduler.release_running_job(job["id"])
    assert key not in scheduler._running_cancel_events


@pytest.mark.parametrize("registered,resumed", [(True, False), (False, False), (True, True)])
def test_pause_before_start_releases_only_its_own_claim(isolated_store, monkeypatch, registered, resumed):
    job = jobs.create_job(prompt="work", schedule="every 5m", deliver="local")
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(claimed, dict)
    runner = Mock(return_value=(True, "output", "[SILENT]", None))
    monkeypatch.setattr(scheduler, "run_job", runner)
    if registered:
        assert scheduler.try_register_running_job(job["id"])
    try:
        receipt = dispatch("pause", job_id=job["id"])
        assert receipt["was_running"] is (not registered)
        assert not receipt["interrupted"]
        if registered:
            shown = jobs.list_jobs(include_disabled=True)[0]
            assert not shown["running"] and shown["running_since"] is None
            assert "warning" not in receipt
        if resumed:
            assert jobs.resume_job(job["id"])
        assert scheduler.run_one_job(claimed)
    finally:
        if registered:
            scheduler.release_running_job(job["id"])
    if resumed:
        runner.assert_called_once()
        assert jobs.get_job(job["id"])["last_status"] == "ok"
        return
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
    scheduler._record_fire_ownership_lost(job, owner, "missing-execution")
    assert jobs.get_job(job["id"])["fire_claim"]["by"] == "replacement"
    from cron.scheduler_control import release_unstarted_claim
    release_unstarted_claim(claimed)
    assert jobs.get_job(job["id"])["fire_claim"]["by"] == "replacement"


@pytest.mark.parametrize("action", ["pause", "remove"])
def test_cancel_after_delivery_keeps_completed_outcome(isolated_store, monkeypatch, action):
    receipt = {}
    operator_context = contextvars.copy_context()
    agent = Mock()
    delivered = Mock(return_value=None)
    monkeypatch.setattr(scheduler, "_deliver_result", delivered)

    def run_job(job, *, defer_agent_teardown, **kwargs):
        defer_agent_teardown.append(agent)
        return True, "output", "sent", None

    def teardown(*args, **kwargs):
        receipt.update(operator_context.run(dispatch, action, job_id=job["id"]))

    monkeypatch.setattr(scheduler, "run_job", run_job)
    monkeypatch.setattr(scheduler, "_teardown_cron_agent", teardown)
    job = jobs.create_job(prompt="work", schedule="every 5m", deliver="telegram:123")
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(claimed, dict)
    assert scheduler.run_one_job(claimed)
    delivered.assert_called_once()
    execution = scheduler.get_execution(claimed["execution_id"])
    assert execution is not None
    assert execution["status"] == "completed"
    assert execution["delivery_outcome"] == "delivered"
    assert "after completed delivery" in execution["output"]
    assert execution["error"] is None
    assert receipt["delivery_completed"] and not receipt["interrupted"]
    if action == "pause":
        current = jobs.get_job(job["id"])
        assert current["state"] == "paused" and current["last_status"] == "ok"
        assert current["last_error"] is None and not current["fire_claim"]


@pytest.mark.parametrize("action,oneshot", [
    ("pause", False), ("pause", True), ("remove", False), ("disable", False), ("disable", True)])
def test_remote_cancel_names_cause_and_preserves_budget(isolated_store, monkeypatch, action, oneshot):
    started, rejected = threading.Event(), threading.Event()
    monkeypatch.setattr(scheduler, "_RUN_CLAIM_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(scheduler, "_FIRE_CLAIM_MISS_CONFIRM_SECONDS", 0.01)
    heartbeat = jobs.heartbeat_fire_claim

    def renew(*args, **kwargs):
        accepted = heartbeat(*args, **kwargs)
        if not accepted:
            rejected.set()
        return accepted

    def run_job(job, **kwargs):
        started.set()
        assert rejected.wait(5)
        return False, "output", "", "fire claim lost"

    monkeypatch.setattr(scheduler, "heartbeat_fire_claim", renew)
    monkeypatch.setattr(scheduler, "run_job", run_job)
    delivery = Mock()
    monkeypatch.setattr(scheduler, "_deliver_result", delivery)
    schedule = (jobs._hermes_now() + timedelta(minutes=5)).isoformat() if oneshot else "every 5m"
    job = jobs.create_job(prompt="work", schedule=schedule, repeat=3, deliver="local")
    jobs.update_job(job["id"], {"failure_streak": 2})
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(contextvars.copy_context().run, scheduler.run_one_job, claimed)
        assert started.wait(5)
        # A detached worker has no control in the operator's process.
        control = scheduler._running_cancel_events[scheduler._inflight_key(job["id"])]
        control.local = False
        if action == "disable":
            updated = jobs.update_job(job["id"], {"enabled": False})
            assert updated is not None and updated["enabled"] is False
        else:
            receipt = dispatch(action, job_id=job["id"])
            assert receipt["was_running"] and not receipt["interrupted"]
        assert not control.is_set()
        assert future.result(timeout=5)
    execution = scheduler.get_execution(claimed["execution_id"])
    assert execution["error"] == scheduler._CANCELLED_INTERRUPTED
    delivery.assert_not_called()
    if action in {"pause", "disable"}:
        current = jobs.get_job(job["id"])
        assert current is not None
        assert current["last_error"] == scheduler._CANCELLED_INTERRUPTED
        assert not current["enabled"] and not current["fire_claim"]
        assert current["failure_streak"] == 2
        assert current["repeat"]["completed"] == int(oneshot)


@pytest.mark.parametrize("failure", ["import", "lookup"])
def test_listing_and_formatting_survive_scheduler_failure(isolated_store, monkeypatch, failure):
    import sys
    job = jobs.create_job(prompt="work", schedule="every 5m", deliver="local")
    if failure == "import":
        monkeypatch.setitem(sys.modules, "cron.scheduler_control", None)
    else:
        monkeypatch.setattr("cron.scheduler_control.running_job_state",
                            Mock(side_effect=RuntimeError("running lock unavailable")))
    listed = jobs.list_jobs()
    assert listed[0]["id"] == job["id"]
    assert listed[0]["running"] is False and listed[0]["running_since"] is None
    from tools.cronjob_job_args import _format_job
    assert _format_job(listed[0])["state"] == "scheduled"


@pytest.mark.parametrize("claimed", [False, True])
def test_listing_without_scheduler_never_imports_it(isolated_store, monkeypatch, claimed):
    import builtins
    import sys
    from tools.cronjob_job_args import _format_job

    job = jobs.create_job(prompt="work", schedule="every 5m", deliver="local")
    if claimed:
        assert jobs.claim_job_for_fire(job["id"])
    monkeypatch.delitem(sys.modules, "cron.scheduler", raising=False)
    original_import = builtins.__import__

    def forbid_scheduler(name, *args, **kwargs):
        if name in {"cron.scheduler", "cron.scheduler_control"}:
            pytest.fail("listing must not boot the scheduler")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", forbid_scheduler)
    listed = jobs.list_jobs()
    assert listed[0]["running"] is claimed
    assert bool(listed[0]["running_since"]) is claimed
    formatted = _format_job(listed[0])
    assert formatted["running"] is claimed
    assert bool(formatted["running_since"]) is claimed
    assert "cron.scheduler" not in sys.modules


def test_cancelled_started_oneshot_keeps_consumed_dispatch(isolated_store, monkeypatch):
    schedule = (jobs._hermes_now() + timedelta(minutes=5)).isoformat()
    job = jobs.create_job(prompt="one-shot", schedule=schedule, deliver="local", repeat=1)
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(claimed, dict)
    operator_context = contextvars.copy_context()

    def run_job(*args, **kwargs):
        current = jobs.get_job(job["id"])
        assert current is not None and current["repeat"]["completed"] == 1
        operator_context.run(jobs.pause_job, job["id"])
        return False, "", "", "paused"

    monkeypatch.setattr(scheduler, "run_job", run_job)
    assert scheduler.run_one_job(claimed)
    persisted = jobs.get_job(job["id"])
    assert persisted is not None
    assert persisted["last_status"] == "error"
    assert persisted["last_run_at"] is not None
    assert persisted["repeat"]["completed"] == 1
    assert persisted["failure_streak"] == 0
    assert not persisted["fire_claim"]
    assert not jobs.claim_dispatch(job["id"]), "a started one-shot needs explicit re-arm, not a refund"


def test_disable_after_delivery_keeps_completed_outcome(isolated_store, monkeypatch):
    delivered = Mock(return_value=None)
    operator_context = contextvars.copy_context()
    agent = Mock()
    monkeypatch.setattr(scheduler, "_deliver_result", delivered)

    def run_job(job, *, defer_agent_teardown, **kwargs):
        defer_agent_teardown.append(agent)
        return True, "output", "sent", None

    def teardown(*args, **kwargs):
        operator_context.run(jobs.update_job, job["id"], {"enabled": False})

    monkeypatch.setattr(scheduler, "run_job", run_job)
    monkeypatch.setattr(scheduler, "_teardown_cron_agent", teardown)
    job = jobs.create_job(prompt="work", schedule="every 5m", deliver="telegram:123")
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(claimed, dict)
    assert scheduler.run_one_job(claimed)
    delivered.assert_called_once()
    execution = scheduler.get_execution(claimed["execution_id"])
    assert execution is not None
    assert execution["status"] == "completed"
    assert execution["delivery_outcome"] == "delivered"
    current = jobs.get_job(job["id"])
    assert current is not None
    assert not current["enabled"] and current["last_status"] == "ok"
    assert current["last_error"] is None and not current["fire_claim"]
