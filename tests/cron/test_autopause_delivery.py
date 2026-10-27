"""Internal scheduler auto-pause must report why it stopped future dispatch."""
import contextvars
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from cron import jobs
from cron import scheduler
import tools.cronjob_tools
from tools.registry import registry


@pytest.mark.parametrize("unrunnable", ["no_agent", "empty_payload"])
def test_unrunnable_claimed_job_autopauses_and_delivers_reason(tmp_path, monkeypatch, unrunnable):
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda job: False)
    monkeypatch.setattr(scheduler, "_RUN_CLAIM_HEARTBEAT_SECONDS", 3600)
    delivered = Mock(return_value=None)
    monkeypatch.setattr(scheduler, "_deliver_result", delivered)
    admitted = Mock(side_effect=AssertionError("an unrunnable job must not need an agent owner"))
    monkeypatch.setattr("cron.scheduler_authority.run_canonical_job", admitted)
    with jobs.use_cron_store(tmp_path):
        job = jobs.create_job(prompt="", schedule="every 5m", no_agent=unrunnable == "no_agent",
                              script="missing.py", deliver="local")
        # A legacy unrunnable record can predate today's create/update validation.
        records = jobs.load_jobs()
        records[0]["script"] = None
        jobs.save_jobs(records)
        claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
        assert isinstance(claimed, dict)
        assert scheduler.run_one_job(claimed)
        paused = jobs.get_job(job["id"])
        assert paused is not None
        assert paused["state"] == "paused"
        assert "Auto-paused by scheduler" in paused["paused_reason"]
        assert paused["last_status"] == "error"
        reason = (jobs.NO_AGENT_WITHOUT_SCRIPT_ERROR if unrunnable == "no_agent"
                  else jobs.EMPTY_PAYLOAD_ERROR)
        assert paused["last_error"] == reason
        assert not paused.get("fire_claim")
        delivered.assert_called_once()
        assert reason in delivered.call_args.args[1]
        assert jobs.get_due_jobs() == []
        admitted.assert_not_called()


def test_run_pausing_own_job_still_delivers_once(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda job: False)
    monkeypatch.setattr(scheduler, "_RUN_CLAIM_HEARTBEAT_SECONDS", 3600)
    delivered = Mock(return_value=None)
    monkeypatch.setattr(scheduler, "_deliver_result", delivered)

    def run_job(job, *, cancel_event, **kwargs):
        paused = jobs.pause_job(job["id"], reason="self pause")
        assert paused is not None and paused["state"] == "paused"
        assert not cancel_event.is_set()
        return True, "output", "completed", None

    monkeypatch.setattr(scheduler, "run_job", run_job)
    with jobs.use_cron_store(tmp_path):
        job = jobs.create_job(prompt="work", schedule="every 5m", deliver="local")
        claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
        assert isinstance(claimed, dict)
        assert scheduler.run_one_job(claimed)
        paused = jobs.get_job(job["id"])
        assert paused is not None
        assert paused["state"] == "paused"
        assert paused["paused_at"]
        assert paused["last_status"] == "ok"
        assert not paused.get("fire_claim")
    delivered.assert_called_once()


def test_operator_pause_after_autopause_still_stops_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda job: False)
    monkeypatch.setattr(scheduler, "_RUN_CLAIM_HEARTBEAT_SECONDS", 3600)
    monkeypatch.setattr(tools.cronjob_tools, "_notify_provider_jobs_changed_safe", lambda: None)
    delivered = Mock(return_value=None)
    monkeypatch.setattr(scheduler, "_deliver_result", delivered)

    def run_job(job, *, cancel_event, **kwargs):
        result = scheduler._block_and_pause_job(job["id"], job["name"], "unrunnable")
        assert not cancel_event.is_set()
        marker_token = jobs._self_removal_delivery.set(None)
        try:
            registry.dispatch("cronjob_manage", {"action": "pause", "job_id": job["id"]})
        finally:
            jobs._self_removal_delivery.reset(marker_token)
        assert cancel_event.is_set()
        return result

    monkeypatch.setattr(scheduler, "run_job", run_job)
    with jobs.use_cron_store(tmp_path):
        job = jobs.create_job(prompt="work", schedule="every 5m", deliver="local")
        claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
        assert isinstance(claimed, dict)
        assert scheduler.run_one_job(claimed)
        paused = jobs.get_job(job["id"])
        assert paused is not None and not paused.get("fire_claim")
    delivered.assert_not_called()


def test_autopause_heartbeat_is_run_local_and_owner_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda job: False)
    monkeypatch.setattr(scheduler, "_RUN_CLAIM_HEARTBEAT_SECONDS", 3600)
    monkeypatch.setattr(scheduler, "_deliver_result", Mock(return_value=None))

    def run_job(job, *, cancel_event, **kwargs):
        result = scheduler._block_and_pause_job(job["id"], job["name"], "unrunnable")
        owner = job["fire_claim"]["by"]
        # The real heartbeat copies the run's context to a sibling thread.
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(contextvars.copy_context().run, jobs.heartbeat_fire_claim,
                               job["id"], expected_owner=owner).result(timeout=5)
        assert not jobs.heartbeat_fire_claim(job["id"], expected_owner="replacement-owner")

        def external_heartbeat():
            with jobs.use_cron_store(tmp_path):
                return jobs.heartbeat_fire_claim(job["id"], expected_owner=owner)

        assert not contextvars.Context().run(external_heartbeat)
        assert not cancel_event.is_set()
        return result

    monkeypatch.setattr(scheduler, "run_job", run_job)
    with jobs.use_cron_store(tmp_path):
        job = jobs.create_job(prompt="work", schedule="every 5m", deliver="local")
        claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
        assert isinstance(claimed, dict)
        assert scheduler.run_one_job(claimed)


def test_reclaimed_paused_owner_never_attempts_terminal_write(tmp_path, monkeypatch):
    mark_run = Mock()
    finish = Mock()
    monkeypatch.setattr(scheduler, "mark_job_run", mark_run)
    monkeypatch.setattr(scheduler, "finish_execution", finish)
    with jobs.use_cron_store(tmp_path):
        job = jobs.create_job(prompt="work", schedule="every 5m", deliver="local")
        claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
        assert isinstance(claimed, dict)
        jobs.pause_job(job["id"])
        scheduler._record_fire_ownership_lost(job, "stale-owner", "stale-execution")
        mark_run.assert_not_called()
        assert finish.call_args.kwargs["error"] == (
            "Fire claim ownership lost; stale result was discarded.")
        current = jobs.get_job(job["id"])
        assert current is not None and current["fire_claim"] == claimed["fire_claim"]
