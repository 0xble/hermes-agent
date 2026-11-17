"""Business-impact priority admission contracts."""

import threading

import pytest

from cron import jobs
from cron import scheduler_admission as admission
from cron.constants import job_priority, job_priority_rank
from cron.scheduler_provider import _profile_cron_scope
from tests.waiting import wait_until


def test_priority_rank_defaults_to_production_and_normalizer_clear():
    assert job_priority({}) == "production"
    assert job_priority_rank({}) == 1
    assert jobs._normalize_priority("live") == "live"
    assert jobs._normalize_priority("background") == "background"
    assert jobs._normalize_priority(None) is None
    assert jobs._normalize_priority_update("") is None
    with pytest.raises(ValueError, match="priority must be one of"):
        jobs._normalize_priority("urgent")


def test_reserved_job_id_prefers_impact_then_store_order(tmp_path, monkeypatch):
    monkeypatch.setattr(admission, "is_protected_interval", lambda job: True)
    monkeypatch.setattr(admission, "hermes_home_key", lambda home: str(home))
    monkeypatch.setattr("cron.jobs.is_job_runnable", lambda job: True)
    monkeypatch.setattr("cron.jobs.is_terminal_job", lambda job: False)
    admission._reserved_claims.clear()
    admission._loaded_protected.clear()
    jobs_in_store = [
        {"id": "background", "priority": "background"},
        {"id": "live", "priority": "live"},
        {"id": "unset"},
    ]
    assert admission.reserved_job_id(jobs_in_store, tmp_path) == "live"
    assert admission.reserved_job_id([
        {"id": "first", "priority": "production"},
        {"id": "second", "priority": "production"},
    ], tmp_path) == "first"
    assert admission.reserved_job_id([
        {"id": "unset"}, {"id": "background", "priority": "background"}
    ], tmp_path) == "unset"


def test_script_pool_priority_is_fifo_within_rank_and_cancels_queued_futures():
    pool = admission.PriorityThreadPoolExecutor(1, thread_name_prefix="priority-test")
    release = threading.Event()
    started = []

    def run(name):
        started.append(name)
        if name == "background-running":
            release.wait(5)
        return name

    first = pool.submit_ranked(2, run, "background-running")
    release.set()
    assert first.result(5) == "background-running"
    # The first run completed; rank then sequence determines the rest.
    live = pool.submit_ranked(0, run, "live")
    production = pool.submit_ranked(1, run, "production")
    background = pool.submit_ranked(2, run, "background")
    assert live.result(5) == "live"
    assert production.result(5) == "production"
    assert background.result(5) == "background"
    assert started[-3:] == ["live", "production", "background"]
    pool.shutdown(wait=True, cancel_futures=True)


def test_legacy_jobs_do_not_persist_priority(tmp_path, monkeypatch):
    with _profile_cron_scope(tmp_path):
        record = jobs.create_job(prompt="legacy", schedule="every 10m")
        assert "priority" not in record
        prioritized = jobs.create_job(prompt="live", schedule="every 10m", priority="live")
        assert prioritized["priority"] == "live"
        updated = jobs.update_job(prioritized["id"], {"priority": ""})
        assert updated is not None
        assert updated.get("priority") is None


def test_live_script_overtakes_a_queued_background_backlog_across_ticks():
    """4 workers busy, 3 background queued from an earlier tick, then a live job arrives:
    the next freed worker runs live first; equal ranks keep FIFO."""
    pool = admission.PriorityThreadPoolExecutor(4, thread_name_prefix="priority-test")
    gates = [threading.Event() for _ in range(4)]
    started = []
    lock = threading.Lock()

    def run(name, gate=None):
        with lock:
            started.append(name)
        if gate is not None:
            assert gate.wait(5)
        return name

    running = [pool.submit_ranked(2, run, f"busy{i}", gates[i]) for i in range(4)]
    assert wait_until(lambda: len(started) == 4, timeout=5)
    queued = [pool.submit_ranked(2, run, f"bg{i}") for i in range(3)]
    live = pool.submit_ranked(0, run, "live")
    gates[0].set()
    assert live.result(5) == "live"
    for gate in gates[1:]:
        gate.set()
    assert [f.result(5) for f in queued] == ["bg0", "bg1", "bg2"]
    assert started[4] == "live" and started[5:] == ["bg0", "bg1", "bg2"]
    assert [f.result(5) for f in running] == [f"busy{i}" for i in range(4)]
    pool.shutdown(wait=True)


def test_shutdown_without_cancel_drains_every_queued_entry():
    """discard/shutdown keep the existing drain contract: queued script futures still run, so
    their completion callbacks release in-flight claims."""
    pool = admission.PriorityThreadPoolExecutor(1, thread_name_prefix="priority-test")
    gate = threading.Event()
    first = pool.submit_ranked(1, gate.wait, 5)
    queued = [pool.submit_ranked(rank, lambda r=rank: r) for rank in (2, 0, 1)]
    pool.shutdown(wait=False, cancel_futures=False)
    gate.set()
    assert first.result(5) is True
    assert sorted(f.result(5) for f in queued) == [0, 1, 2]
    with pytest.raises(RuntimeError):
        pool.submit_ranked(0, lambda: None)


def test_submit_due_jobs_orders_one_tick_by_rank_and_keeps_fifo_within_rank():
    order = []
    pool = admission.PriorityThreadPoolExecutor(1, thread_name_prefix="priority-test")
    due = [{"id": "bg1", "priority": "background"}, {"id": "unset"},
           {"id": "live", "priority": "live"}, {"id": "bg2", "priority": "background"},
           {"id": "prod", "priority": "production"}]
    assert [j["id"] for j in sorted(due, key=job_priority_rank)] == [
        "live", "unset", "prod", "bg1", "bg2"]
    for job in sorted(due, key=job_priority_rank):
        admission.submit_ranked(pool, job, order.append, job["id"])
    pool.shutdown(wait=True)
    assert order == ["live", "unset", "prod", "bg1", "bg2"]


def test_plain_pools_ignore_rank():
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        assert admission.submit_ranked(pool, {"priority": "live"}, lambda: 7).result(5) == 7


@pytest.mark.parametrize("priority, expected", [("live", "live"), (None, "production"),
                                                ("background", "background")])
def test_script_subprocess_gets_work_priority_and_never_inherits_it(
        tmp_path, monkeypatch, priority, expected):
    from cron import scheduler_script

    from cron import scheduler

    monkeypatch.setenv("HERMES_WORK_PRIORITY", "live")
    scripts = scheduler._get_hermes_home() / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    script = scripts / "print_priority.py"
    script.write_text("import os; print(os.environ.get('HERMES_WORK_PRIORITY'))\n", encoding="utf-8")
    ok, output = scheduler_script._run_job_script(
        "print_priority.py", workdir=str(tmp_path), priority=job_priority({"priority": priority}))
    assert ok, output
    assert output.strip() == expected
    assert "HEAVY_SLOT_URGENCY" not in output
