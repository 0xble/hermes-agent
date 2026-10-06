"""Background delegations cannot outlive the admission proof used to retire their process."""

import threading
import time


def test_queued_admission_requeues_when_retirement_fence_closes(monkeypatch):
    from hermes_cli import backend_retirement
    from tools import async_delegation
    monkeypatch.setattr(async_delegation, "_STALE_CHECK_INTERVAL", 0.01)

    class Fence:
        def __init__(self):
            self.reject = False
            self.active = 0

        def acquire(self):
            if self.reject:
                return False
            self.active += 1
            return True

        def release(self):
            self.active -= 1

        def work(self):
            fence = self

            class Work:
                def __enter__(self):
                    admitted = fence.acquire()
                    self.admitted = admitted
                    return admitted

                def __exit__(self, *_args):
                    if self.admitted:
                        fence.release()

            return Work()

    fence = Fence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    release = threading.Event()
    queued_started = threading.Event()
    first = async_delegation.dispatch_async_delegation(
        goal="occupy", context=None, toolsets=None, role="leaf", model="m", session_key="",
        runner=lambda: (release.wait(30), {"status": "completed"})[1], max_async_children=1,
    )
    queued = async_delegation.dispatch_async_delegation(
        goal="stay queued", context=None, toolsets=None, role="leaf", model="m", session_key="",
        runner=lambda: (queued_started.set(), {"status": "completed"})[1], max_async_children=1,
        max_queued_delegations=1,
    )
    assert first["status"] == "dispatched"
    assert queued["status"] == "queued"
    fence.reject = True
    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = next((r for r in async_delegation.list_async_delegations()
                       if r["delegation_id"] == queued["delegation_id"]), None)
        if record is not None and record["status"] in {"failed", "error"}:
            break
        time.sleep(0.02)
    record = next(r for r in async_delegation.list_async_delegations()
                  if r["delegation_id"] == queued["delegation_id"])
    assert record["status"] in {"failed", "error"}
    assert not queued_started.is_set()
    assert fence.active == 0
    assert async_delegation.active_count() == 0
    async_delegation._reset_for_tests()




def test_retirement_fence_failure_terminally_settles_unsubmitted_siblings(monkeypatch):
    """A closed retirement fence settles every unsubmitted sibling, not just the first."""
    from tools import async_delegation

    release = threading.Event()
    first = async_delegation.dispatch_async_delegation(
        goal="occupy", context=None, toolsets=None, role="leaf", model="m", session_key="",
        runner=lambda: (release.wait(30), {"status": "completed"})[1], max_async_children=1,
    )
    counts = [0, 0]
    slot_key = "retiring-sibling-group"
    handles = [
        async_delegation.dispatch_async_delegation_batch(
            goals=[f"sibling-{i}"], context=None, toolsets=None, role="leaf", model="m", session_key="",
            slot_key=slot_key,
            runner=lambda i=i: (counts.__setitem__(i, counts[i] + 1),
                                {"results": [{"task_index": 0, "status": "completed"}]})[1],
            max_async_children=1, max_queued_delegations=1,
        )
        for i in range(2)
    ]
    assert first["status"] == "dispatched"
    assert [handle["status"] for handle in handles] == ["queued", "queued"]

    monkeypatch.setattr(async_delegation, "_submit_record", lambda *_args: async_delegation._BACKEND_RETIRING)
    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with async_delegation._records_lock:
            statuses = [async_delegation._records[h["delegation_id"]]["status"] for h in handles]
            if all(status in async_delegation._TERMINAL_STATES for status in statuses):
                break
        time.sleep(0.01)
    with async_delegation._records_lock:
        assert all(async_delegation._records[h["delegation_id"]]["status"] == "failed" for h in handles)
        assert all(not async_delegation._records[h["delegation_id"]].get("_future") for h in handles)
        assert slot_key not in async_delegation._active_slots_locked()
    assert counts == [0, 0]
    async_delegation._reset_for_tests()


def test_monitor_exit_race_wakes_for_queue_admitted_during_final_sweep(monkeypatch):
    """A queue arriving while the monitor decides to exit must not strand."""
    from tools import async_delegation

    monkeypatch.setattr(async_delegation, "_STALE_CHECK_INTERVAL", 0.001)
    sweep_entered = threading.Event()
    release_sweep = threading.Event()
    first_sweep = threading.Event()

    def empty_sweep(_now):
        if first_sweep.is_set():
            return [], [], False
        first_sweep.set()
        sweep_entered.set()
        release_sweep.wait(5)
        return [], [], False

    monkeypatch.setattr(async_delegation, "_sweep_stale_locked", empty_sweep)
    first_release = threading.Event()
    first = async_delegation.dispatch_async_delegation(
        goal="occupy", context=None, toolsets=None, role="leaf", model="m", session_key="",
        runner=lambda: (first_release.wait(10), {"status": "completed"})[1], max_async_children=1,
        progress_fn=lambda: (0, False),
    )
    assert first["status"] == "dispatched"
    assert sweep_entered.wait(5)
    queued_started = threading.Event()
    queued = async_delegation.dispatch_async_delegation(
        goal="race queue", context=None, toolsets=None, role="leaf", model="m", session_key="",
        runner=lambda: (queued_started.set(), {"status": "completed"})[1], max_async_children=1,
        max_queued_delegations=1,
    )
    assert queued["status"] == "queued"
    release_sweep.set()
    first_release.set()
    assert queued_started.wait(5), "monitor exited with queued work pending"
    deadline = time.monotonic() + 5
    while async_delegation.active_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert async_delegation.active_count() == 0
    async_delegation._reset_for_tests()


def test_async_delegations_hold_busy_accounting_through_finalization(tmp_path, monkeypatch):
    from hermes_cli import backend_retirement
    from hermes_cli.web_server_idle_proof import idle_proof
    from tools import async_delegation

    fence = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    entered, release = threading.Event(), threading.Event()

    def runner():
        entered.set()
        assert release.wait(10)
        return {"summary": "test done"}

    def dispatch():
        return async_delegation.dispatch_async_delegation(
            goal="test", context=None, toolsets=None, role="leaf", model=None,
            session_key="test", runner=runner)

    token = fence.prepare()["token"]
    try:
        assert dispatch()["status"] == "rejected"
        assert not entered.is_set()
        assert fence.cancel(token) == {"ok": True}
        handle = dispatch()
        assert handle["status"] == "dispatched"
        assert entered.wait(10)
        assert async_delegation.active_count() > 0
        assert idle_proof()["idle"] is False
        assert fence.prepare() == {"ok": False, "idle": False}
        # A stalled record may be force-finalized before its stuck runner really unwinds.
        async_delegation._finalize(handle["delegation_id"], {"error": "test stall"}, "stalled")
        assert async_delegation.active_count() == 0
        assert fence.prepare() == {"ok": False, "idle": False}
    finally:
        release.set()
        if async_delegation._executor is not None:
            async_delegation._executor.shutdown(wait=True)
    assert fence.prepare()["ok"] is True
