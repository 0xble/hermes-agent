"""The suite's phase hooks wait for the shared-metrics worker before pytest's next phase begins.

Pytest starts each phase by iterating the live ``logging.Logger.manager.loggerDict``
(``catching_logs.__enter__``). A metrics job still running from the previous phase can register
a logger during that loop and raise ``RuntimeError: dictionary changed size during iteration``
(hosted job 114104725116). These tests pin that the conftest drain waits for in-flight work, and
that draining never starts a worker nobody asked for.
"""

import logging
import threading

from hermes_cli.observability import shared_metrics_gateway as smg
from tests import conftest as suite_conftest


def test_phase_drain_waits_for_an_in_flight_job_that_registers_loggers():
    release = threading.Event()
    running = threading.Event()
    name = "tests.metrics_phase_drain.in_flight"
    logging.Logger.manager.loggerDict.pop(name, None)

    def job():
        running.set()
        assert release.wait(5)
        logging.getLogger(name)

    smg._submit(job)
    assert running.wait(5)
    # Release only after the drain is already waiting, so a drain that returned early
    # would see the logger missing.
    threading.Timer(0.2, release.set).start()
    suite_conftest._drain_shared_metrics_worker()
    assert name in logging.Logger.manager.loggerDict
    logging.Logger.manager.loggerDict.pop(name, None)


def test_drain_without_a_worker_returns_without_starting_one(monkeypatch):
    monkeypatch.setattr(smg, "_executor", None)
    smg.drain()
    assert smg._executor is None


def test_phase_hooks_drain_around_setup_call_and_teardown(monkeypatch):
    drains = []
    monkeypatch.setattr(suite_conftest, "_drain_shared_metrics_worker", lambda: drains.append(1))

    class Item:
        @staticmethod
        def get_closest_marker(name):
            return None

    for hook, args in (
        (suite_conftest.pytest_runtest_setup, (Item(),)),
        (suite_conftest.pytest_runtest_call, (Item(),)),
        (suite_conftest.pytest_runtest_teardown, (Item(), None)),
    ):
        gen = hook(*args)
        next(gen)
        try:
            gen.send(None)
        except StopIteration:
            pass
    assert len(drains) == 3
