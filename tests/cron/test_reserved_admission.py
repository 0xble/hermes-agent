"""Completion-cadence admission with saturated general workers and detached-process fencing."""

import errno
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cron import executions, jobs, scheduler
from cron import scheduler_admission as admission
from cron.constants import PROTECTED_INTERVAL_HEADROOM_SECONDS
from cron.scheduler_provider import InProcessCronScheduler, _profile_cron_scope
from hermes_constants import hermes_home_key
from tests.waiting import wait_until


@pytest.fixture
def clock_home(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=datetime(2030, 1, 1, tzinfo=UTC), elapsed=0.0)
    monkeypatch.setattr(jobs, "_hermes_now", lambda: clock.now)
    monkeypatch.setattr(executions, "_hermes_now", lambda: clock.now)
    monkeypatch.setattr(scheduler, "_sweep_stale_inflight_for_tick", lambda *args: None)
    monkeypatch.setattr(scheduler, "_should_yield_tick_to_fresh_gateway", lambda: None)
    monkeypatch.setattr(scheduler, "_sweep_mcp_orphans", lambda: None)
    monkeypatch.setattr(scheduler, "_maybe_run_worktree_maintenance", lambda: None)
    monkeypatch.setattr(scheduler, "load_config", lambda: {"cron": {}})
    monkeypatch.setattr(scheduler, "cron_env_setting", lambda name: "")
    assert scheduler._resolve_max_parallel_workers() == 4
    admission._wake.clear()
    with _profile_cron_scope(tmp_path):
        yield clock
        scheduler._shutdown_parallel_pool()
        admission._reserved_claims.clear()
        admission._loaded_protected.clear()
        admission._wake.clear()


def _process_until_exit(exited):
    process = Mock(pid=4321)
    process.poll.side_effect = lambda: 0 if exited.is_set() else None

    def wait(timeout=None):
        if not exited.wait(timeout):
            assert timeout is not None
            raise subprocess.TimeoutExpired("cron-worker", timeout)
        return 0

    process.wait.side_effect = wait
    return process


def make_job(schedule="every 2m"):
    return jobs.create_job(prompt="scheduled work", schedule=schedule, deliver="local")


def set_due(job, when):
    jobs.update_job(job["id"], {"next_run_at": when.isoformat()})
    result = jobs.get_job(job["id"])
    assert result is not None
    return result


def test_general_release_cannot_clear_fresh_reservation_for_same_job(clock_home, monkeypatch):
    job = make_job()
    key = hermes_home_key(scheduler._get_hermes_home())
    general_owner = object()
    assert scheduler.try_register_running_job(job["id"], owner=general_owner)
    original_submit = scheduler._submit_with_guard

    def submit(item, pool, process_job, **kwargs):
        # A newly selected reservation exists before its running registration. The
        # prior overflow run can finish in that gap without owning the reservation.
        scheduler.release_running_job(item["id"], owner=general_owner)
        assert key in admission._reserved_claims
        return original_submit(item, pool, process_job, **kwargs)

    monkeypatch.setattr(scheduler, "_submit_with_guard", submit)
    future = admission.submit_reserved(job, lambda item: True)
    assert future is not None
    assert future.result(timeout=5) is True
    assert key not in admission._reserved_claims


def test_saturated_general_workers_start_protected_by_completion_deadline(clock_home, monkeypatch):
    """Run the real ticker, store, claims, executors and started_at ledger against a fake clock."""
    clock = clock_home
    blockers = [make_job("every 10m") for _ in range(5)]
    protected = make_job()
    previous = executions.create_execution(protected["id"], source="builtin")
    executions.mark_execution_running(previous["id"])
    finished = executions.finish_execution(previous["id"], success=True)
    jobs.mark_job_run(protected["id"], True)
    for item in blockers:
        set_due(item, clock.now)
    release = threading.Event()
    general_started = []
    protected_started = threading.Event()
    starts_lock = threading.Lock()
    next_execution = []

    def run(item, **kw):
        row = executions.mark_execution_running(item["execution_id"])
        if item["id"] == protected["id"]:
            next_execution.append(row)
            jobs.mark_job_run(item["id"], True)
            executions.finish_execution(item["execution_id"], success=True)
            protected_started.set()
        else:
            with starts_lock:
                general_started.append(item["id"])
            assert release.wait(10)
            jobs.mark_job_run(item["id"], True)
            executions.finish_execution(item["execution_id"], success=True)
        return True

    monkeypatch.setattr(scheduler, "run_one_job", run)
    assert scheduler.tick(sync=False) == len(blockers)
    assert wait_until(lambda: len(general_started) == 4, timeout=5)
    admission._wake.clear()

    class Stop:
        def is_set(self):
            return protected_started.is_set()

        def wait(self, seconds):
            clock.elapsed += seconds
            clock.now += timedelta(seconds=seconds)
            # Let a submitted reserved callback finish before advancing the fake wall clock.
            if clock.elapsed > 90:
                assert protected_started.wait(5)
            return self.is_set()

    fake_time = SimpleNamespace(monotonic=lambda: clock.elapsed)
    monkeypatch.setattr(admission, "time", fake_time)
    monkeypatch.setattr("cron.scheduler_provider.time", fake_time)
    monkeypatch.setattr(InProcessCronScheduler, "recover_interrupted", lambda self: 0)
    try:
        InProcessCronScheduler().start(Stop(), interval=60)
        started = datetime.fromisoformat(next_execution[0]["started_at"])
        completed = datetime.fromisoformat(finished["finished_at"])
        assert (started - completed).total_seconds() <= 120
        assert len(general_started) == 4  # fifth general job still queued, never used the reserve
        assert len(admission._reserved_pools) == 1
        assert scheduler.tick(sync=False) == 0  # the queued general registration is not duplicated
    finally:
        release.set()
    assert wait_until(lambda: len(general_started) == 5, timeout=5)
    assert general_started[4] == blockers[4]["id"]


def test_second_protected_job_is_logged_and_uses_general_fifo(clock_home, monkeypatch, caplog):
    first, second = make_job(), make_job("every 1m")
    ordinary = make_job("every 10m")
    for item in (first, ordinary, second):
        set_due(item, clock_home.now)
    release = threading.Event()
    starts = {}
    submissions = []
    original_submit = scheduler._submit_with_guard

    def submit(item, pool, process_job, **kwargs):
        submissions.append(item["id"])
        return original_submit(item, pool, process_job, **kwargs)

    monkeypatch.setattr(scheduler, "_submit_with_guard", submit)

    def run(item, **kw):
        starts[item["id"]] = threading.current_thread().name
        assert release.wait(10)
        return True

    monkeypatch.setattr(scheduler, "run_one_job", run)
    try:
        assert scheduler.tick(sync=False) == 3
        assert wait_until(lambda: len(starts) == 3, timeout=5)
        assert starts[first["id"]].startswith("cron-reserved")
        assert starts[second["id"]].startswith("cron-parallel")
        assert starts[ordinary["id"]].startswith("cron-parallel")
        assert submissions == [first["id"], second["id"], ordinary["id"]]
        for _ in range(3):
            admission.reserved_job_id(jobs.load_jobs(), scheduler._get_hermes_home())
        errors = [r for r in caplog.records if "only 1 reserved worker" in r.message]
        assert len(errors) == 1
        assert second["id"] in errors[0].message
        assert len(admission._reserved_pools) == 1
    finally:
        release.set()


@pytest.mark.parametrize("inactive", [
    {"enabled": False}, {"state": "paused"}, {"paused_at": "2030-01-01T00:00:00+00:00"},
    {"state": "completed"}, {"state": "error"},
])
def test_inactive_protected_job_does_not_take_reservation(clock_home, monkeypatch, caplog, inactive):
    first, active = make_job(), make_job()
    first.update(inactive)
    monkeypatch.setattr(jobs, "load_jobs", lambda: [first, active])
    started = []
    futures = admission.submit_due_jobs(
        [active], scheduler._get_parallel_pool(4),
        lambda item: started.append(threading.current_thread().name))
    assert len(futures) == 1
    futures[0].result(5)
    assert started[0].startswith("cron-reserved")
    assert not any("only 1 reserved worker" in record.message for record in caplog.records)


def test_sweep_returns_reserved_slot_after_detached_process_exit(clock_home):
    protected = make_job()
    exited = threading.Event()
    process = _process_until_exit(exited)
    key = scheduler._inflight_key(protected["id"])
    owners = []

    def handed_off(item):
        with scheduler._running_lock:
            owner = scheduler._running_registration_owners[key]
            owners.append(owner)
            scheduler._external_worker_claims[key] = (owner, process)
        executions.finish_execution(item["execution_id"], success=True)
        return True

    try:
        future = admission.submit_reserved(protected, handed_off)
        assert future is not None
        assert future.result(5)
        # A finished waiter and terminal ledger row still cannot release a live process.
        assert scheduler.sweep_stale_inflight([protected]) == []
        assert admission.submit_reserved(protected, lambda item: True) is None
        exited.set()
        assert scheduler.sweep_stale_inflight([protected]) == [protected["id"]]
        assert not scheduler.is_job_running(protected["id"])
        # The late reaper sees no matching owner after the sweep has removed the claim.
        scheduler.release_running_job(protected["id"], owner=owners[0], force=True)
        successor = admission.submit_reserved(protected, lambda item: True)
        assert successor is not None
        assert successor.result(5)
    finally:
        exited.set()
        scheduler.release_running_job(protected["id"], owner=owners[0], force=True)


def test_long_protected_run_has_one_instance_and_rearms_from_completion(clock_home, monkeypatch):
    protected = set_due(make_job(), clock_home.now)
    release, started = threading.Event(), threading.Event()
    runs = []

    def run(item, **kw):
        runs.append(item["execution_id"])
        started.set()
        assert release.wait(10)
        jobs.mark_job_run(item["id"], True)
        executions.finish_execution(item["execution_id"], success=True)
        return True

    monkeypatch.setattr(scheduler, "run_one_job", run)
    try:
        assert scheduler.tick(sync=False) == 1
        assert started.wait(5)
        clock_home.now += timedelta(minutes=10)
        for _ in range(3):
            assert scheduler.tick(sync=False) == 0
        assert len(runs) == 1
        # No general worker is occupied by a long protected run.
        assert scheduler._get_parallel_pool(4).submit(lambda: "general-free").result(5) == "general-free"
    finally:
        release.set()
    assert wait_until(lambda: not scheduler.is_job_running(protected["id"]), timeout=5)
    due = datetime.fromisoformat(jobs.get_job(protected["id"])["next_run_at"])
    assert (due - clock_home.now).total_seconds() == 120 - PROTECTED_INTERVAL_HEADROOM_SECONDS


@pytest.mark.parametrize("schedule", ["every 1m", "every 2m", "every 3m", "*/2 * * * *", "in 2m"])
def test_only_protected_completion_rearm_is_earlier(clock_home, schedule):
    from cron.constants import PROTECTED_INTERVAL_HEADROOM_SECONDS, is_protected_interval

    item = make_job(schedule)
    original = jobs.compute_next_run(item["schedule"], clock_home.now.isoformat())
    jobs.mark_job_run(item["id"], True)
    actual = jobs.get_job(item["id"])
    if original is not None and item["schedule"]["kind"] != "once":
        offset = (datetime.fromisoformat(original) - datetime.fromisoformat(actual["next_run_at"])).total_seconds()
        assert offset == (PROTECTED_INTERVAL_HEADROOM_SECONDS if is_protected_interval(item) else 0)
    else:
        assert actual["next_run_at"] is None


def test_reserved_slot_survives_post_handoff_waiter_error_until_process_exit(clock_home, monkeypatch):
    """A terminal ledger row and a finished future must not free the reserved worker."""
    protected = make_job()
    exited = threading.Event()
    process = _process_until_exit(exited)
    monkeypatch.setattr(scheduler, "get_execution", Mock(side_effect=RuntimeError("ledger read failed")))

    def launch(item):
        with scheduler._running_lock:
            scheduler._external_worker_modes[scheduler._inflight_key(item["id"])] = "detached"
        return scheduler._wait_for_external_cron_worker(
            process, execution_id=item["execution_id"], job_id=item["id"])

    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", launch)
    try:
        future = admission.submit_reserved(protected, launch)
        with pytest.raises(scheduler._ExternalWorkerPostHandoffError):
            future.result(5)
        assert scheduler.is_job_running(protected["id"])
        # Even changing the selected job cannot allocate a second reserved slot.
        replacement = make_job()
        assert admission.submit_reserved(replacement, lambda item: True) is None
        assert hermes_home_key(scheduler._get_hermes_home()) in admission._reserved_claims
        exited.set()
        assert wait_until(lambda: not scheduler.is_job_running(protected["id"]), timeout=5)
        replacement_future = admission.submit_reserved(replacement, lambda item: True)
        assert replacement_future.result(5)
    finally:
        exited.set()


def test_terminal_row_keeps_reserved_worker_until_process_exit(clock_home, monkeypatch):
    protected = make_job()
    exited, observed_terminal = threading.Event(), threading.Event()
    process = _process_until_exit(exited)

    def terminal_row(execution_id):
        observed_terminal.set()
        return {"id": execution_id, "status": "completed"}

    monkeypatch.setattr(scheduler, "get_execution", terminal_row)

    def launch(item):
        with scheduler._running_lock:
            scheduler._external_worker_modes[scheduler._inflight_key(item["id"])] = "detached"
        return scheduler._wait_for_external_cron_worker(
            process, execution_id=item["execution_id"], job_id=item["id"])

    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", launch)
    try:
        future = admission.submit_reserved(protected, launch)
        assert observed_terminal.wait(5)
        assert process.poll() is None
        assert scheduler.is_job_running(protected["id"])
        assert not future.done()
        assert admission.submit_reserved(make_job(), lambda item: True) is None
        exited.set()
        assert future.result(5)
        assert wait_until(lambda: not scheduler.is_job_running(protected["id"]), timeout=5)
    finally:
        exited.set()


def test_due_wait_uses_earliest_served_home_and_ignores_running(clock_home, tmp_path):
    item = set_due(make_job(), clock_home.now + timedelta(seconds=40))
    other_home = tmp_path / "other"
    other_home.mkdir()
    with _profile_cron_scope(other_home):
        set_due(make_job("every 10m"), clock_home.now + timedelta(seconds=17))
    homes = [(None, tmp_path), ("other", other_home)]
    assert admission.due_wait_for_homes(60, homes) == 17
    assert admission.next_due_wait(60) == 40
    assert scheduler.try_register_running_job(item["id"])
    try:
        assert admission.next_due_wait(60) == 60
    finally:
        scheduler.release_running_job(item["id"])


def test_worker_release_interrupts_deadline_wait(clock_home):
    stopped = threading.Event()
    finished = threading.Event()
    admission._wake.clear()
    thread = threading.Thread(target=lambda: (admission.wait_for_tick(stopped, 60), finished.set()))
    thread.start()
    try:
        assert scheduler.try_register_running_job("wake-slot")
        scheduler.release_running_job("wake-slot")
        assert finished.wait(5)
    finally:
        stopped.set()
        thread.join(5)


def _run_clocked_ticker(clock, monkeypatch, tick, stopped, *, home=None, can_dispatch=None):
    fake_time = SimpleNamespace(monotonic=lambda: clock.elapsed)
    monkeypatch.setattr(admission, "time", fake_time)
    monkeypatch.setattr("cron.scheduler_provider.time", fake_time)
    monkeypatch.setattr(scheduler, "tick", tick)
    monkeypatch.setattr(InProcessCronScheduler, "recover_interrupted", lambda self: 0)

    class Stop:
        def is_set(self):
            return stopped()

        def wait(self, seconds):
            assert seconds >= 0
            if not self.is_set():
                clock.elapsed += seconds
                clock.now += timedelta(seconds=seconds)
            return self.is_set()

    homes = [("test", home)] if home is not None else None
    InProcessCronScheduler().start(
        Stop(), interval=60, profile_homes=homes, can_dispatch=can_dispatch)


@pytest.mark.parametrize("loop", ["single", "multiplex"])
@pytest.mark.parametrize("gate", ["estop", "dispatch_blocked", "drain", "tick_lock", "run_claim"])
def test_gated_overdue_job_retains_heartbeat_cadence(clock_home, tmp_path, monkeypatch, loop, gate):
    from agent import estop
    from cron import store_health

    item = set_due(make_job("in 2m" if gate == "run_claim" else "every 2m"), clock_home.now)
    can_dispatch = None
    if gate == "estop":
        monkeypatch.setattr(estop, "_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(estop, "_canonical_root", lambda: tmp_path)
        estop.engage()
    elif gate == "dispatch_blocked":
        monkeypatch.setattr(store_health, "dispatch_blocked", lambda due: True)
    elif gate == "drain":
        can_dispatch = lambda: False
    elif gate == "tick_lock":
        monkeypatch.setattr(scheduler, "_acquire_tick_lock", lambda path: None)
    else:
        jobs.update_job(item["id"], {
            "run_claim": {"at": clock_home.now.isoformat(), "by": "another-process"}})
    heartbeat, clear_error, run = Mock(), Mock(), Mock()
    monkeypatch.setattr(jobs, "record_ticker_heartbeat", heartbeat)
    monkeypatch.setattr(jobs, "clear_ticker_error", clear_error)
    monkeypatch.setattr(scheduler, "run_one_job", run)
    ticks = []
    original_tick = scheduler.tick

    def tick(**kwargs):
        ticks.append(clock_home.elapsed)
        return original_tick(**kwargs)

    _run_clocked_ticker(
        clock_home, monkeypatch, tick, lambda: clock_home.elapsed >= 60,
        home=tmp_path if loop == "multiplex" else None, can_dispatch=can_dispatch)
    assert len(ticks) <= 2, f"{len(ticks)} ticks in 60s while {gate}: {ticks[:5]}"
    assert heartbeat.call_count <= 3  # startup plus at most 2 periodic ticks
    assert clear_error.call_count <= 2
    run.assert_not_called()
    stored = jobs.get_job(item["id"])
    assert stored is not None
    assert stored["next_run_at"] == item["next_run_at"]


@pytest.mark.parametrize("stage", ["create_execution", "note_cron_execution", "submit"])
def test_unstarted_dispatch_cleanup_does_not_wake_ticker(clock_home, monkeypatch, stage):
    item = set_due(make_job(), clock_home.now)
    pool = Mock()
    failure = OSError(errno.EMFILE, "Too many open files")
    if stage == "submit":
        pool.submit.side_effect = failure
    else:
        monkeypatch.setattr(scheduler, stage, Mock(side_effect=failure))
    assert scheduler._submit_with_guard(item, pool, Mock()) is None
    assert not scheduler.is_job_running(item["id"])
    assert not admission._wake.is_set()


@pytest.mark.parametrize("loop", ["single", "multiplex"])
def test_persistent_execution_creation_failure_does_not_spin(clock_home, tmp_path, monkeypatch, loop):
    """A failed dispatch does not masquerade as newly freed worker capacity."""
    set_due(make_job(), clock_home.now)
    create = Mock(side_effect=OSError(errno.EMFILE, "Too many open files"))
    monkeypatch.setattr(scheduler, "create_execution", create)
    original_tick = scheduler.tick
    ticks = []

    def tick(**kwargs):
        ticks.append(clock_home.elapsed)
        result = original_tick(**kwargs)
        # Bookkeeping takes a little time, but must not cause an immediate retry.
        clock_home.elapsed += 0.01
        clock_home.now += timedelta(seconds=0.01)
        return result

    _run_clocked_ticker(
        clock_home, monkeypatch, tick, lambda: clock_home.elapsed >= 3 or len(ticks) >= 400,
        home=tmp_path if loop == "multiplex" else None)
    assert create.call_count >= 1
    assert len(ticks) <= 4, f"{len(ticks)} ticks in a 3-second fake-clock window: {ticks[:5]}"
    assert not admission._wake.is_set()


@pytest.mark.parametrize("loop", ["single", "multiplex"])
def test_deadline_crossed_during_tick_is_scanned_within_floor(clock_home, tmp_path, monkeypatch, loop):
    clock_home.elapsed = 60.0
    clock_home.now += timedelta(seconds=60)
    set_due(make_job(), clock_home.now + timedelta(seconds=0.2))
    starts, ticks = [], []

    def run(item, **kwargs):
        starts.append(clock_home.elapsed)
        jobs.mark_job_run(item["id"], True)
        executions.finish_execution(item["execution_id"], success=True)
        return True

    monkeypatch.setattr(scheduler, "run_one_job", run)
    original_tick = scheduler.tick

    def tick(**kwargs):
        ticks.append(clock_home.elapsed)
        result = original_tick(sync=True)
        if len(ticks) == 1:
            clock_home.elapsed += 0.25
            clock_home.now += timedelta(seconds=0.25)
        return result

    _run_clocked_ticker(
        clock_home, monkeypatch, tick, lambda: len(ticks) >= 2,
        home=tmp_path if loop == "multiplex" else None)
    assert ticks == pytest.approx([60.0, 61.25])
    assert starts == pytest.approx([61.25])


def test_reselected_protected_job_uses_general_fifo_while_reserve_is_held(clock_home, monkeypatch, caplog):
    first = set_due(make_job(), clock_home.now)
    ordinary = make_job("every 10m")
    release, started = threading.Event(), threading.Event()
    starts, submissions = {}, []
    original_submit = scheduler._submit_with_guard

    def submit(item, pool, process_job, **kwargs):
        submissions.append(item["id"])
        return original_submit(item, pool, process_job, **kwargs)

    def run(item, **kwargs):
        starts[item["id"]] = threading.current_thread().name
        if item["id"] == first["id"]:
            started.set()
        assert release.wait(10)
        return True

    monkeypatch.setattr(scheduler, "_submit_with_guard", submit)
    monkeypatch.setattr(scheduler, "run_one_job", run)
    try:
        assert scheduler.tick(sync=False) == 1
        assert started.wait(5)
        jobs.update_job(first["id"], {"enabled": False})
        replacement = make_job()
        for item in (ordinary, replacement):
            set_due(item, clock_home.now)
        assert scheduler.tick(sync=False) == 2
        assert wait_until(lambda: len(starts) == 3, timeout=5)
        assert submissions == [first["id"], ordinary["id"], replacement["id"]]
        assert starts[first["id"]].startswith("cron-reserved")
        assert starts[ordinary["id"]].startswith("cron-parallel")
        assert starts[replacement["id"]].startswith("cron-parallel")
        key = hermes_home_key(scheduler._get_hermes_home())
        assert admission._reserved_claims[key][0] == first["id"]
        assert len(admission._reserved_pools) == 1
        assert scheduler.tick(sync=False) == 0
        overflow = [record for record in caplog.records if "general pool best-effort" in record.message]
        assert len(overflow) == 1
    finally:
        release.set()
