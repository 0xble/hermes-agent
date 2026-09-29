"""Overlap must reject an external cron provider before claiming gateway resources."""
import asyncio
from types import SimpleNamespace

import pytest


def test_external_cron_refused_before_pid_and_adapter_connect(monkeypatch):
    from cron import scheduler_provider
    from gateway import run
    import gateway.code_skew
    import gateway.status
    import hermes_cli.resource_limits

    config = SimpleNamespace(overlap_handover_enabled=True)
    monkeypatch.setattr(run, "_host_attach_or_none", lambda *_: asyncio.sleep(0, result=None))
    monkeypatch.setattr(gateway.status, "get_running_pid", lambda: None)
    monkeypatch.setattr(run, "_start_gateway_configure_logging", lambda *_: None)
    monkeypatch.setattr(run, "_enable_multiplex_log_routing", lambda *_: None)
    monkeypatch.setattr(run, "_cron_tick_profile_homes", lambda *_: [])
    monkeypatch.setattr(run, "GatewayRunner", lambda *_: SimpleNamespace(config=config))
    monkeypatch.setattr(run, "_start_gateway_claim_pid_file", lambda **_: pytest.fail("PID claimed before cron preflight"))
    monkeypatch.setattr(hermes_cli.resource_limits, "apply_nofile_soft_limit", lambda: None)
    monkeypatch.setattr(gateway.code_skew, "record_boot_fingerprint", lambda: None)
    monkeypatch.setattr(scheduler_provider, "resolve_cron_scheduler", lambda: SimpleNamespace(name="external"))

    with pytest.raises(RuntimeError, match="overlap requires an in-process cron ticker"):
        asyncio.run(run.start_gateway(config))
