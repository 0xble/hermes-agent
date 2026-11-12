"""Regression tests for CLI gateway run exit behavior.

``hermes gateway run`` enters through hermes_cli.gateway, not gateway.run.main().
After graceful teardown it must use the same hard-exit backstop as gateway.run.main()
so Python finalization does not wait on non-daemon worker threads (for example
in-flight cron ThreadPoolExecutor jobs) and delay service-managed restarts.
"""

from __future__ import annotations

import asyncio
import types

import pytest


class _HardExitObserved(BaseException):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def _prepare(monkeypatch):
    import hermes_cli.gateway as gateway_cli
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_cli, "_guard_official_docker_root_gateway", lambda: None)
    monkeypatch.setattr(gateway_cli, "_guard_named_profile_under_multiplexer", lambda force=False: None)
    monkeypatch.setattr(gateway_cli, "_attach_to_host_gateway_or_guard", lambda **kwargs: None)
    monkeypatch.setattr(gateway_cli, "_guard_supervised_gateway_conflict", lambda force=False: None)
    monkeypatch.setattr(gateway_cli, "_guard_existing_gateway_process_conflict", lambda replace=False: None)
    monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway_cli.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    monkeypatch.setenv("HERMES_GATEWAY_EXIT_DIAG", "0")

    async def _start_gateway(*args, **kwargs):  # pragma: no cover - never awaited by fake run
        return True

    def _hard_exit(code: int) -> None:
        raise _HardExitObserved(code)

    monkeypatch.setattr(gateway_run, "start_gateway", _start_gateway)
    monkeypatch.setattr(gateway_run, "_exit_after_graceful_shutdown", _hard_exit)
    return gateway_cli


@pytest.mark.parametrize("outcome", [True, False, SystemExit(78), KeyboardInterrupt(), asyncio.CancelledError()])
def test_run_gateway_records_exit_before_hard_exit(monkeypatch, tmp_path, outcome):
    import json
    import gateway.run as gateway_run

    gateway_cli = _prepare(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_GATEWAY_EXIT_DIAG", "1")
    monkeypatch.setattr(gateway_cli, "_respawn_storm_backoff", lambda: None)

    async def start_gateway(**kwargs):
        try:
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        finally:
            (tmp_path / "durable-stop").write_text("persisted")

    observed = []

    def hard_exit(code):
        assert (tmp_path / "durable-stop").read_text() == "persisted"
        records = [json.loads(line) for line in (tmp_path / "logs" / "gateway-exit-diag.log").read_text().splitlines()]
        observed.extend(records)
        raise _HardExitObserved(code)

    monkeypatch.setattr(gateway_run, "start_gateway", start_gateway)
    monkeypatch.setattr(gateway_run, "_exit_after_graceful_shutdown", hard_exit)
    with pytest.raises(_HardExitObserved) as excinfo:
        gateway_cli.run_gateway()
    expected_code = 78 if isinstance(outcome, SystemExit) else 1 if outcome is False else 0
    assert excinfo.value.code == expected_code
    tags = [record["tag"] for record in observed]
    if isinstance(outcome, SystemExit):
        assert "asyncio.run.SystemExit" in tags
        assert next(record for record in observed if record["tag"] == "asyncio.run.SystemExit")["code"] == 78
    elif isinstance(outcome, KeyboardInterrupt):
        assert "asyncio.run.KeyboardInterrupt" in tags
    elif isinstance(outcome, BaseException):
        assert "asyncio.run.exception" in tags
    else:
        assert "asyncio.run.returned" in tags
        assert ("gateway.exit_clean" if outcome else "gateway.exit_nonzero") in tags
