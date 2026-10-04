"""Background delegations cannot outlive the admission proof used to retire their process."""

import threading
import time


def test_queued_admission_requeues_when_retirement_fence_closes(monkeypatch):
    from hermes_cli import backend_retirement
    from tools import async_delegation

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
    first = async_delegation.dispatch_async_delegation(
        goal="occupy", context=None, toolsets=None, role="leaf", model="m", session_key="",
        runner=lambda: (release.wait(30), {"status": "completed"})[1], max_async_children=1,
    )
    queued = async_delegation.dispatch_async_delegation(
        goal="stay queued", context=None, toolsets=None, role="leaf", model="m", session_key="",
        runner=lambda: {"status": "completed"}, max_async_children=1,
    )
    assert first["status"] == "dispatched"
    assert queued["status"] == "queued"
    fence.reject = True
    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any(r["delegation_id"] == queued["delegation_id"] and r["status"] == "queued"
               for r in async_delegation.list_async_delegations()):
            break
        time.sleep(0.02)
    assert any(r["delegation_id"] == queued["delegation_id"] and r["status"] == "queued"
               for r in async_delegation.list_async_delegations())
    deadline = time.monotonic() + 5
    while fence.active and time.monotonic() < deadline:
        time.sleep(0.02)
    assert fence.active == 0
    fence.reject = False
    async_delegation._admit_pending()
    deadline = time.monotonic() + 5
    while async_delegation.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
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
