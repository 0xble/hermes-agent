"""Dead claims cannot pin an interrupted one-shot or misclassify an unstarted arm."""
import json
import os
import socket
import subprocess
import sys
from datetime import timedelta

import pytest

from cron import delivery_queue, executions, jobs
from cron.jobs_oneshot import occurrence_started
from tests.cron.oneshot_helpers import NOW, store


@pytest.mark.parametrize("late", [60, 600])
@pytest.mark.parametrize("spent", [False, True])
@pytest.mark.parametrize("owner", ["dead", "expired", "live"])
def test_restart_settles_dead_claims_without_replay(store, monkeypatch, late, spent, owner):
    due = (NOW - timedelta(seconds=late)).isoformat()
    if owner == "dead":
        child = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                               capture_output=True, text=True, check=True)
        pid = int(child.stdout.strip())
    else:
        pid = os.getpid()
    at = NOW - timedelta(seconds=jobs.FIRE_CLAIM_TTL_SECONDS + jobs._oneshot_run_claim_ttl_seconds() + 1) if owner == "expired" else NOW
    claim = {"at": at.isoformat(), "by": f"{socket.gethostname()}:{pid}"}
    job = {"id": "crashed", "prompt": "keep this payload", "enabled": True, "state": "scheduled",
           "schedule": {"kind": "once", "run_at": due}, "next_run_at": due,
           "repeat": {"times": 1, "completed": int(spent)}, "deliver": "local",
           "run_claim": claim, "fire_claim": claim}
    jobs.save_jobs([job])
    assert occurrence_started(job) is (spent or owner == "live")
    for _ in range(3):
        due_jobs = jobs.get_due_jobs()
        if owner == "live":
            assert due_jobs == []
            assert jobs.get_job(job["id"])["enabled"] is True
        elif spent:
            assert due_jobs == []
            assert jobs.get_job(job["id"]) is None
            receipt = delivery_queue.get_status("oneshot-retired:crashed")
            assert receipt and "interrupted" in receipt["content"] and "partial effects" in receipt["content"]
        elif late > jobs.ONESHOT_GRACE_SECONDS:
            assert due_jobs == []
            held = jobs.get_job(job["id"])
            assert held["state"] == "missed" and held["enabled"] is False
            assert held["prompt"] == job["prompt"]
            assert delivery_queue.get_status(f"oneshot-missed:crashed:{due}")
        else:
            assert [j["id"] for j in due_jobs] == ([job["id"]] if _ == 0 else [])
    if owner != "live" and spent:
        assert executions.list_executions(job_id=job["id"]) == []


@pytest.mark.parametrize("failure_lane", [None, "local", "telegram:owner-alerts"])
def test_held_miss_respects_owner_failure_lane(store, failure_lane):
    due = (NOW - timedelta(seconds=600)).isoformat()
    job = {"id": "held", "prompt": "payload", "enabled": True, "state": "scheduled",
           "schedule": {"kind": "once", "run_at": due}, "next_run_at": due,
           "repeat": {"times": 1, "completed": 0}, "deliver": "local",
           "failure_deliver": failure_lane}
    jobs.save_jobs([job])
    assert jobs.get_due_jobs() == []
    notice = json.loads(delivery_queue.get_status(f"oneshot-missed:held:{due}")["job_json"])
    if failure_lane and failure_lane != "local":
        assert notice["failure_deliver"] == failure_lane
    else:
        assert not notice.get("failure_deliver") and notice["deliver"] == "origin"
