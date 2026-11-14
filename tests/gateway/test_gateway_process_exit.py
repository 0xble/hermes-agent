import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import gateway.run as gateway_run


class _ExitCalled(Exception):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def _raise_exit(code: int) -> None:
    raise _ExitCalled(code)


@pytest.mark.parametrize(
    ("shutdown_requested", "requested_code"),
    [(False, None), (True, 0), (True, 75)],
    ids=["stray-cancellation", "sigterm-shutdown", "restart"],
)
def test_cancelled_process_exit_uses_recorded_shutdown_request(
    monkeypatch, shutdown_requested, requested_code
):
    from gateway.run_process_exit import current_process_exit_state, run_gateway_process

    observed = []

    async def start_gateway():
        state = current_process_exit_state()
        assert state is not None
        state.update(shutdown_requested=shutdown_requested, exit_code=requested_code)
        raise asyncio.CancelledError()

    monkeypatch.setattr(gateway_run, "_exit_after_graceful_shutdown", observed.append)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_gateway_process(start_gateway))

    assert observed == [requested_code if shutdown_requested else 1]


@pytest.mark.parametrize(
    ("service_restart", "expected_code"), [(False, 0), (True, 75)],
    ids=["detached-or-in-place", "service"],
)
def test_cancelled_process_exit_reads_runner_shutdown_flags(monkeypatch, service_restart, expected_code):
    from gateway.run_bootstrap import _record_process_exit_state
    from gateway.run_process_exit import run_gateway_process

    async def start_gateway():
        runner = SimpleNamespace(
            _restart_requested=True,
            _restart_via_service=service_restart,
            _signal_initiated_shutdown=False,
            _shutdown_event=asyncio.Event(),
            exit_code=None,
        )
        _record_process_exit_state(runner)
        raise asyncio.CancelledError()

    observed = []
    monkeypatch.setattr(gateway_run, "_exit_after_graceful_shutdown", observed.append)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_gateway_process(start_gateway))
    assert observed == [expected_code]


def test_main_terminates_via_os_exit_not_systemexit(monkeypatch):
    """The terminating call must be os._exit, NOT sys.exit — SystemExit is
    exactly what triggers the Py_FinalizeEx non-daemon-thread join hang this
    fixes (#53107). If main() ever regresses to sys.exit(), SystemExit would
    propagate instead of our os._exit sentinel and this test would fail.

    Test contributed by @AgenticSpark (PR #53122, duplicate of #53121)."""
    async def fake_start_gateway(config=None):
        return False

    stdout = SimpleNamespace(flush=Mock())
    stderr = SimpleNamespace(flush=Mock())

    monkeypatch.setattr("hermes_cli.boot_bootstrap.maybe_run_boot_bootstrap", lambda _root: None)
    monkeypatch.setattr(gateway_run, "start_gateway", fake_start_gateway)
    monkeypatch.setattr(gateway_run.os, "_exit", _raise_exit)
    monkeypatch.setattr(gateway_run.sys, "argv", ["gateway.run"])
    monkeypatch.setattr(gateway_run.sys, "stdout", stdout)
    monkeypatch.setattr(gateway_run.sys, "stderr", stderr)

    # Our os._exit sentinel must be what terminates main() — not SystemExit.
    with pytest.raises(_ExitCalled):
        gateway_run.main()


def test_main_routes_systemexit_through_os_exit(monkeypatch):
    """start_gateway raises SystemExit on the clean-fatal-config (#51228),
    planned-restart, and service-restart paths. main() must catch it and route
    the carried code through os._exit too, so those paths are equally wedge-proof
    (#53107) — a SystemExit propagating to interpreter finalization would join a
    stuck non-daemon worker and hang. Verifies the explicit code (e.g. 78) is
    preserved through the os._exit backstop."""
    async def fake_start_gateway(config=None):
        raise SystemExit(78)

    stdout = SimpleNamespace(flush=Mock())
    stderr = SimpleNamespace(flush=Mock())

    monkeypatch.setattr("hermes_cli.boot_bootstrap.maybe_run_boot_bootstrap", lambda _root: None)
    monkeypatch.setattr(gateway_run, "start_gateway", fake_start_gateway)
    monkeypatch.setattr(gateway_run.os, "_exit", _raise_exit)
    monkeypatch.setattr(gateway_run.sys, "argv", ["gateway.run"])
    monkeypatch.setattr(gateway_run.sys, "stdout", stdout)
    monkeypatch.setattr(gateway_run.sys, "stderr", stderr)

    with pytest.raises(_ExitCalled) as exc_info:
        gateway_run.main()

    # The SystemExit(78) must be converted to os._exit(78), not propagated.
    assert exc_info.value.code == 78
    stdout.flush.assert_called_once_with()
    stderr.flush.assert_called_once_with()


def test_exit_backstop_releases_pid_file_and_runtime_lock(monkeypatch):
    """os._exit bypasses atexit, and the early SystemExit exit paths never run
    _stop_impl — so the force-exit backstop itself must release the PID file and
    runtime lock, or those early paths (#51228 fatal-config) would leak them.
    Both releases are idempotent, so this is safe on every exit path."""
    from gateway import status as gateway_status

    remove_pid = Mock()
    release_lock = Mock()
    monkeypatch.setattr(gateway_status, "remove_pid_file", remove_pid)
    monkeypatch.setattr(gateway_status, "release_gateway_runtime_lock", release_lock)
    monkeypatch.setattr(gateway_run.os, "_exit", _raise_exit)
    monkeypatch.setattr(gateway_run.sys, "stdout", SimpleNamespace(flush=Mock()))
    monkeypatch.setattr(gateway_run.sys, "stderr", SimpleNamespace(flush=Mock()))

    with pytest.raises(_ExitCalled) as exc_info:
        gateway_run._exit_after_graceful_shutdown(78)

    assert exc_info.value.code == 78
    remove_pid.assert_called_once_with()
    release_lock.assert_called_once_with()
