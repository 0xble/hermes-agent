"""Cancellation and registration fencing must compose with upstream retry accounting."""
from concurrent.futures import Future
from unittest.mock import Mock

import pytest

from cron import jobs, scheduler
from cron.occurrences import scheduled_instant


def test_replaced_registration_keeps_its_future_and_cancellation_control(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_interpreter_shutting_down", lambda *args: False)
    monkeypatch.setattr(scheduler, "note_cron_execution", lambda job: None)
    with jobs.use_cron_store(tmp_path):
        job = jobs.create_job(prompt="work", schedule="every 2h", deliver="local")
        key = scheduler._inflight_key(job["id"])
        replacement_owner, replacement_future, stale_future = object(), Future(), Future()
        submitted = []

        def submit(run):
            submitted.append(run)
            original_owner = scheduler._running_registration_owners[key]
            scheduler.release_running_job(job["id"], owner=original_owner)
            assert scheduler.try_register_running_job(
                job["id"], owner=replacement_owner, future=replacement_future)
            return stale_future

        try:
            assert scheduler._submit_with_guard(job, Mock(submit=submit), lambda job: True) is stale_future
            replacement_control = scheduler._running_cancel_events[key]
            assert scheduler._running_futures[key] is replacement_future
            assert submitted[0]() is True  # stale worker's finally cannot release its successor
            assert scheduler._running_registration_owners[key] is replacement_owner
            assert scheduler._running_cancel_events[key] is replacement_control
            assert scheduler._running_futures[key] is replacement_future
            jobs.pause_job(job["id"])
            assert replacement_control.is_set()
        finally:
            scheduler.release_running_job(job["id"], owner=replacement_owner)
        assert key not in scheduler._running_cancel_events


@pytest.mark.parametrize("schedule_stop", [False, True])
def test_interrupted_retry_does_not_spend_another_occurrence(tmp_path, schedule_stop):
    with jobs.use_cron_store(tmp_path):
        job = jobs.create_job(prompt="work", schedule="every 2h", repeat=3, deliver="local")
        retry_at = jobs._hermes_now().isoformat()
        jobs.update_job(job["id"], {
            "repeat": {"times": 3, "completed": 1}, "failure_streak": 2,
            "unreachable_retry": {"attempt": 1, "at": retry_at},
        })
        claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
        assert isinstance(claimed, dict)
        claimed["_scheduled_instant"] = scheduled_instant(retry_at)
        owner = claimed["fire_claim"]["by"]
        execution = scheduler.create_execution(job["id"], source="manual")
        claimed["execution_id"] = execution["id"]
        if schedule_stop:
            jobs.pause_job(job["id"])
        scheduler._record_fire_ownership_lost(claimed, owner, claimed["execution_id"])
        current = jobs.get_job(job["id"])
        assert current["repeat"]["completed"] == 1
        assert current["failure_streak"] == (2 if schedule_stop else 3)
        assert current["state"] == ("paused" if schedule_stop else "scheduled")
        assert not current["fire_claim"]
        execution = scheduler.get_execution(claimed["execution_id"])
        assert execution["status"] == "failed"
        assert execution["error"] == (
            scheduler._CANCELLED_INTERRUPTED if schedule_stop else scheduler._OWNERSHIP_LOST_INTERRUPTED)
