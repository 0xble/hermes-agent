"""Supervised gateway acknowledgement stays responsive through slow boots."""

import asyncio
import os
import threading

import pytest

from hermes_cli import gateway, immutable_releases
from gateway import status


@pytest.mark.platforms("macos")
def test_supervised_gateway_ack_retries_after_long_boot_without_blocking_loop(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: tmp_path)
    checks = []
    main_thread = threading.get_ident()

    def read_state():
        checks.append(1)
        return {"pid": os.getpid(), "gateway_state": "running"} if len(checks) > 120 else {}

    def acknowledge(home, *, gateway_pid):
        assert home == tmp_path and gateway_pid == os.getpid()
        assert threading.get_ident() != main_thread
        return True

    monkeypatch.setattr(status, "read_runtime_status", read_state)
    monkeypatch.setattr(immutable_releases, "acknowledge_running_release", acknowledge)
    asyncio.run(asyncio.wait_for(gateway._acknowledge_release_when_running(poll_seconds=0), timeout=3))
    assert len(checks) > 120


@pytest.mark.platforms("macos")
def test_supervised_gateway_ack_retries_later_running_state(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: tmp_path)
    observations = iter([{"pid": os.getpid(), "gateway_state": "running"},
                         {"pid": os.getpid(), "gateway_state": "stopping"},
                         {"pid": os.getpid(), "gateway_state": "running"}])
    monkeypatch.setattr(status, "read_runtime_status", lambda: next(observations))
    attempts = []
    monkeypatch.setattr(immutable_releases, "acknowledge_running_release",
                        lambda *args, **kwargs: attempts.append(1) or len(attempts) == 2)
    asyncio.run(asyncio.wait_for(gateway._acknowledge_release_when_running(poll_seconds=0), timeout=3))
    assert attempts == [1, 1]
