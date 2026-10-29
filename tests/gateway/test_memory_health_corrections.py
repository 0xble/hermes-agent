"""Advisory health must not hold gateway startup or lose provider diagnostics."""
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway import runtime_health as health


@pytest.mark.parametrize("metadata", ["missing", "invalid-requirement"])
def test_optional_metadata_cannot_hide_provider_reason(tmp_path, monkeypatch, caplog, metadata):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("memory: {provider: fixture}\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("plugins.memory.load_memory_provider", lambda *args, **kwargs: SimpleNamespace(
        is_available=lambda: False, unavailable_reason=lambda: "SDK client is missing"))
    inputs = Mock(side_effect=ValueError("bad optional manifest")) if metadata == "missing" else Mock(
        return_value=({"python_dependencies": ["not a requirement !!!"]}, {}))
    monkeypatch.setattr("hermes_cli.memory_setup.memory_provider_dependency_inputs", inputs)
    assert "SDK client is missing" in health._readiness_issue()
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]

@pytest.mark.asyncio
@pytest.mark.parametrize("invariant", ["offloop", "routing"])
async def test_scope_discovery_is_off_loop_and_uses_served_primary_home(tmp_path, monkeypatch, invariant):
    import asyncio
    import threading
    from unittest.mock import AsyncMock
    from hermes_constants import get_hermes_home

    primary, launch = tmp_path / "primary", tmp_path / "launch"
    primary.mkdir()
    launch.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    loop_thread = threading.get_ident()
    scans, checked = [], []

    def served(config):
        scans.append(threading.get_ident())
        return [("default", primary), ("launch", launch)]

    async def check(runner, profile, home):
        checked.append((profile, home, get_hermes_home()))

    monkeypatch.setattr("gateway.run._multiplex_profile_homes", served)
    monkeypatch.setattr(health, "_check_profile_health", check)
    gateway = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True),
                              _run_in_executor_with_context=AsyncMock())
    await health.check_memory_health(gateway)
    if invariant == "offloop":
        assert scans and all(thread != loop_thread for thread in scans)
    else:
        assert checked == [(None, primary, primary), ("launch", launch, launch)]

@pytest.mark.asyncio
async def test_startup_does_not_await_blocked_advisory_health(tmp_path, monkeypatch):
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(runner):
        entered.set()
        await release.wait()

    monkeypatch.setattr(health, "check_memory_health", blocked)
    monkeypatch.setattr("gateway.cron_store_notices.install_cron_store_notices", Mock())
    gateway = SimpleNamespace()
    setup = asyncio.create_task(health.install_health_notices(gateway))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert setup.done(), "advisory health blocked the startup wiring task"
        future = gateway._memory_health_future
        health.schedule_memory_health(gateway, asyncio.get_running_loop())
        assert gateway._memory_health_future is future
    finally:
        release.set()
        await setup
        if hasattr(gateway, "_memory_health_future"):
            await asyncio.wrap_future(gateway._memory_health_future)


@pytest.mark.asyncio
async def test_failing_profile_tracebacks_are_deduped_per_release(tmp_path, monkeypatch, caplog):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(health, "_health_scopes", lambda _: [("broken", tmp_path)])
    monkeypatch.setattr(health, "_check_profile_health", AsyncMock(side_effect=ValueError("bad config")))
    monkeypatch.setattr("hermes_cli.version_info.get_code_identity", lambda: {"sha": "A"})
    gateway = SimpleNamespace()
    caplog.set_level(logging.DEBUG, logger="gateway.runtime_health")
    await health.check_memory_health(gateway)
    await health.check_memory_health(gateway)
    failures = [r for r in caplog.records if "Memory readiness check failed" in r.getMessage()]
    assert [r.levelno for r in failures] == [logging.ERROR, logging.DEBUG]
    assert all(r.exc_info for r in failures)
    monkeypatch.setattr("hermes_cli.version_info.get_code_identity", lambda: {"sha": "B"})
    await health.check_memory_health(gateway)
    assert caplog.records[-1].levelno == logging.ERROR

@pytest.mark.parametrize("name", ["default", "builtin", "built-in", "none", "  BUILTIN  "])
def test_core_memory_selection_never_enters_external_probes(tmp_path, monkeypatch, name):
    from hermes_cli import immutable_release_preflight as preflight
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(f"memory: {{provider: '{name}'}}\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    loader = Mock(side_effect=AssertionError("core store entered plugin loader"))
    monkeypatch.setattr("plugins.memory.load_memory_provider", loader)
    monkeypatch.setattr(preflight, "_verify_extras", lambda *args: [])
    probe = Mock(side_effect=AssertionError("core store entered external subprocess"))
    monkeypatch.setattr(preflight, "_probe", probe)
    preflight.activation_preflight(tmp_path, None, home)
    assert health._readiness_issue() == ""
    loader.assert_not_called()
    probe.assert_not_called()
