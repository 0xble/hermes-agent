"""A real in-flight poll must not prevent an identity-safe stop."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.tools.test_named_node_execution import call, peers  # noqa: F401

pytestmark = pytest.mark.platforms("posix")


def test_stop_reaches_original_supervisor_before_inflight_poll_returns(peers, monkeypatch):
    *_, registry, _ = peers
    launched = call("terminal", {"node": "macstudio", "command": "printf ready; sleep 60", "background": True})
    session = registry._lookup_session(launched["session_id"])
    poll_inflight, release_poll, stopped = (threading.Event() for _ in range(3))
    original_request = registry._ssh_request
    receipts = []

    def request(handle, action, **kwargs):
        record = original_request(handle, action, **kwargs)
        assert handle.ssh_binding == session.ssh_binding
        if action == "inspect" and not poll_inflight.is_set():
            poll_inflight.set()
            assert release_poll.wait(15), "in-flight poll was not released"
        elif action == "stop":
            receipts.append(record)
            stopped.set()
        return record

    monkeypatch.setattr(registry, "_ssh_request", request)
    with ThreadPoolExecutor(1, thread_name_prefix="long-poll") as polls, ThreadPoolExecutor(1) as kills:
        polling = polls.submit(call, "process", {"action": "wait", "session_id": session.id, "timeout": 15})
        try:
            assert poll_inflight.wait(8), "public wait never reached the remote inspect boundary"
            killing = kills.submit(call, "process", {"action": "kill", "session_id": session.id})
            assert stopped.wait(4), "the original supervisor must receive stop before the poll releases its lock"
            assert not release_poll.is_set() and not polling.done()
            assert receipts[0]["token"] == session.ssh_binding.token
            assert receipts[0]["state"] == "exited" and receipts[0]["reason"] == "killed"
        finally:
            release_poll.set()
        result = killing.result(timeout=10)
        waited = polling.result(timeout=10)
    assert result["status"] in {"killed", "already_exited"}, result
    assert session.exited and session.completion_reason == "killed"
    assert waited["status"] == "exited", waited
    assert result["node"] == "macstudio" and result["ssh"] == "macstudio"
