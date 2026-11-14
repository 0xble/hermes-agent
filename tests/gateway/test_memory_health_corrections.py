"""Advisory memory health must stay bounded and preserve actionable diagnostics."""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
async def test_scope_discovery_is_off_loop_and_uses_served_primary_home(tmp_path, monkeypatch):
    primary, launch = tmp_path / "primary", tmp_path / "launch"
    primary.mkdir()
    launch.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    checked = []

    async def check(runner, profile, home):
        checked.append((profile, home))

    monkeypatch.setattr("gateway.run._handoff_watch_scopes", Mock(side_effect=AssertionError("sync resolver used")))
    monkeypatch.setattr("gateway.run._resolve_handoff_watch_scopes", AsyncMock(return_value=[(None, None), ("launch", launch)]))
    monkeypatch.setattr(health, "_check_profile_health", check)
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: primary)
    gateway = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True),
                              _run_in_executor_with_context=AsyncMock(side_effect=lambda fn, runner: fn(runner)))
    await health.check_memory_health(gateway)
    assert checked == [(None, primary), ("launch", launch)]


@pytest.mark.asyncio
async def test_startup_does_not_await_blocked_advisory_health(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(runner):
        entered.set()
        await release.wait()

    monkeypatch.setattr(health, "check_memory_health", blocked)
    monkeypatch.setattr("gateway.cron_store_notices.install_cron_store_notices", Mock())
    gateway = SimpleNamespace()
    setup = asyncio.create_task(health.install_health_notices(gateway))
    await asyncio.wait_for(entered.wait(), 2)
    assert setup.done()
    release.set()
    await setup
    await asyncio.wrap_future(gateway._memory_health_future)


@pytest.mark.asyncio
async def test_failing_profile_tracebacks_are_deduped_per_release(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("gateway.run._resolve_handoff_watch_scopes", AsyncMock(return_value=[("broken", tmp_path)]))
    monkeypatch.setattr(health, "_check_profile_health", AsyncMock(side_effect=ValueError("bad config")))
    monkeypatch.setattr("hermes_cli.version_info.get_code_identity", lambda: {"sha": "A"})
    gateway = SimpleNamespace()
    caplog.set_level(logging.DEBUG, logger="gateway.runtime_health")
    await health.check_memory_health(gateway)
    await health.check_memory_health(gateway)
    failures = [r for r in caplog.records if "Memory readiness check failed" in r.getMessage()]
    assert [r.levelno for r in failures] == [logging.ERROR, logging.DEBUG]
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
    monkeypatch.setattr(preflight, "_verify_extras", lambda *args, **kwargs: [])
    probe = Mock(side_effect=AssertionError("core store entered external subprocess"))
    monkeypatch.setattr(preflight, "_probe", probe)
    preflight.activation_preflight(tmp_path, None, home)
    assert health._readiness_issue() == ""
    loader.assert_not_called()
    probe.assert_not_called()
