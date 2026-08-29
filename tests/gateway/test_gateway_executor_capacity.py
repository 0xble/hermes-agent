"""Regression coverage for gateway worker-pool starvation."""

from __future__ import annotations

import threading

from gateway.run import GatewayRunner


def _bare_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._executor_lock = threading.Lock()
    runner._executor = None
    runner._executor_closing = False
    return runner


def test_gateway_executor_keeps_capacity_beyond_ten_long_turns() -> None:
    """An eleventh turn must not queue behind the historical ten-worker cap."""
    runner = _bare_runner()
    release = threading.Event()
    started = [threading.Event() for _ in range(11)]
    futures = []

    def blocker(index: int) -> None:
        started[index].set()
        release.wait(timeout=5)

    try:
        executor = runner._get_executor()
        futures = [executor.submit(blocker, index) for index in range(11)]
        assert all(event.wait(timeout=1) for event in started)
    finally:
        release.set()
        for future in futures:
            future.result(timeout=2)
        runner._shutdown_executor()
