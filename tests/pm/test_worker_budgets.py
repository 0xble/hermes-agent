"""The protocol ceiling covers every sequential command, including compile retry."""
from contextlib import contextmanager

import pytest


@pytest.mark.parametrize(("operation", "arguments", "minimum"), [
    ("build_environment", {"frozen": False, "timeout": 2400}, 120 + 3 * 2400 + 60 + 180),
    ("build_environment", {"frozen": True}, 120 + 2 * 1800 + 60 + 180),
    ("ensure_environment", {"timeout": 2400}, 120 + 3 * 2400 + 60 + 180),
    ("ensure_python_tool", {"timeout": 2400}, 120 + 3 * 2400 + 60 + 180),
    ("ensure_project_environment", {}, 120 + 2 * 1800 + 60 + 180),
    ("sync_venv", {"plugins": {"kind": "members", "paths": []}}, 120 + 3 * 1800 + 60 + 180),
    ("ensure", {"name": "venv"}, 120 + 3 * 1800 + 60 + 180),
    ("build_requirements_environment", {}, 120 + 1800 + 60 + 180),
    ("lock_project", {}, 1800 + 180),
    ("check_project_lock", {}, 1800 + 180),
    ("export_requirements", {}, 1800 + 180),
    ("prune_cache", {"cache": "unused"}, 1800 + 180),
    ("venv_is_current", {}, 180),
    ("ensure", {"name": "uv"}, 180),
    ("stage_only", {"name": "uv"}, 180),
    ("prepare_tools", {"names": ["uv"]}, 180),
    ("stage_tools", {"names": ["uv"]}, 180),
])
def test_request_deadline_covers_operation_steps(monkeypatch, operation, arguments, minimum):
    from pm import client
    import pm.client_process as processes

    class Captured(Exception):
        pass

    @contextmanager
    def capture(command, *, timeout, **kwargs):
        assert timeout >= minimum
        raise Captured
        yield  # pragma: no cover

    monkeypatch.setattr(client, "_worker_command", lambda *args: [])
    monkeypatch.setattr(processes, "worker_process", capture)
    with pytest.raises(Captured):
        client._request(operation, arguments)


@pytest.mark.parametrize("plugin_count", [0, 1, 7])
def test_eviction_reserves_every_possible_member_build(plugin_count):
    from pm.worker_budget import operation_budget, reporting, reserve_eviction_builds

    reports = []
    with reporting(reports.append):
        reserve_eviction_builds(plugin_count)
    total = operation_budget("sync_venv", {}) + reports[0]["additional"]
    assert total >= (2 * plugin_count + 4) * (120 + 3 * 1800 + 60 + 60) + 180


def test_unbounded_preparation_preserves_remaining_command_budget(monkeypatch):
    import threading
    import pm.client_process as processes
    from pm.worker_budget import reporting, unbounded

    now = [0]
    timers = []

    class Timer:
        def __init__(self, seconds, callback):
            self.seconds = seconds
            self.callback = callback
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            pass

        def join(self):
            pass

    monkeypatch.setattr(processes.threading, "Timer", Timer)
    monkeypatch.setattr(processes.time, "monotonic", lambda: now[0])
    deadline = processes.WorkerDeadline(6000, threading.Event())
    deadline.start(None)

    def report(message):
        if message["suspended"]:
            deadline.suspend()
        else:
            deadline.resume()

    with reporting(report):
        now[0] = 100
        with unbounded():
            now[0] = 100000
            with unbounded():
                assert len(timers) == 1
    assert timers[-1].seconds == 5900
    assert not deadline.expired.is_set()
