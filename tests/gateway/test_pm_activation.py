"""Gateway startup remains fail-open when PM admission itself fails.

Healthy/damaged real-store activation belongs to tests/pm/test_startup_activation.py.
"""
from unittest.mock import AsyncMock

import pytest


def test_gateway_main_survives_pm_failure(monkeypatch, tmp_path):
    monkeypatch.setattr("pm.paths.install_root", lambda: tmp_path)
    import pm
    import gateway.run as gateway

    failed = []
    def broken():
        failed.append("activate")
        raise RuntimeError("PM unavailable")

    monkeypatch.setattr(pm, "activate", broken)
    started = AsyncMock(return_value=True)
    exited = []

    class ProcessExited(BaseException):
        pass

    def exit_process(code):
        exited.append(code)
        # Match os._exit's nonreturning contract: it now runs before Runner.close.
        raise ProcessExited

    monkeypatch.setattr(gateway, "start_gateway", started)
    monkeypatch.setattr(gateway, "_exit_after_graceful_shutdown", exit_process)
    monkeypatch.setattr("hermes_cli.boot_bootstrap.maybe_run_boot_bootstrap", lambda root: None)
    monkeypatch.setattr("sys.argv", ["gateway"])
    with pytest.raises(ProcessExited):
        gateway.main()
    started.assert_awaited_once()
    assert exited == [0]
    assert failed == ["activate"]
