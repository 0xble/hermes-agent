"""Boot and periodic memory checks share provider readiness and durable release dedupe."""
import importlib.metadata
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.memory import hindsight


def setup_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "hindsight").mkdir(parents=True)
    (home / "config.yaml").write_text("memory: {provider: hindsight}\n")
    (home / "hindsight/config.json").write_text(json.dumps({"mode": "local_external"}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("hermes_cli.version_info.get_code_identity", lambda: {"sha": "release-A"})
    original = importlib.metadata.version

    def missing(name):
        if name == "hindsight-client":
            raise importlib.metadata.PackageNotFoundError(name)
        return original(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    monkeypatch.setattr(hindsight.HindsightMemoryProvider, "initialize",
                        lambda *args: pytest.fail("readiness initialized a provider"))
    monkeypatch.setattr("pm.sync_venv", lambda *args, **kwargs: pytest.fail("readiness installed dependencies"))
    return home


def runner():
    return SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=False),
        _served_home_channel_transports=lambda: [(None, "fixture", None, "operator", object())],
        _send_home_channel_message=AsyncMock(return_value=True))


@pytest.mark.asyncio
async def test_boot_and_health_ticks_emit_one_error_and_notice_per_release(tmp_path, monkeypatch, caplog):
    from gateway.runtime_health import check_memory_health, schedule_memory_health
    import asyncio
    setup_home(tmp_path, monkeypatch)
    caplog.set_level(logging.ERROR, logger="gateway.runtime_health")
    gateway = runner()
    await check_memory_health(gateway)
    gateway._send_home_channel_message.assert_awaited_once()
    message = gateway._send_home_channel_message.call_args.args[3]
    assert "Hindsight client is missing" in message and "hermes pm install" in message
    assert len(caplog.records) == 1
    # The periodic entry point uses the same dedupe, including across restart.
    schedule_memory_health(gateway, asyncio.get_running_loop())
    await asyncio.wrap_future(gateway._memory_health_future)
    restarted = runner()
    await check_memory_health(restarted)
    restarted._send_home_channel_message.assert_not_awaited()
    assert len(caplog.records) == 1
    monkeypatch.setattr("hermes_cli.version_info.get_code_identity", lambda: {"sha": "release-B"})
    await check_memory_health(restarted)
    restarted._send_home_channel_message.assert_awaited_once()
    assert len(caplog.records) == 2


@pytest.mark.asyncio
async def test_missing_client_after_boot_is_detected_and_unsent_notice_retries(tmp_path, monkeypatch, caplog):
    from gateway.runtime_health import check_memory_health
    home = setup_home(tmp_path, monkeypatch)
    caplog.set_level(logging.ERROR, logger="gateway.runtime_health")
    gateway = runner()
    (home / "config.yaml").write_text("memory: {}\n")
    await check_memory_health(gateway)
    assert not caplog.records
    (home / "config.yaml").write_text("memory: {provider: hindsight}\n")
    gateway._send_home_channel_message.return_value = False
    await check_memory_health(gateway)
    gateway._send_home_channel_message.return_value = True
    await check_memory_health(gateway)
    await check_memory_health(gateway)
    assert gateway._send_home_channel_message.await_count == 2
    assert len(caplog.records) == 1


@pytest.mark.asyncio
async def test_missing_credentials_are_not_reported_as_client_loss(tmp_path, monkeypatch, caplog):
    from gateway.runtime_health import check_memory_health
    setup_home(tmp_path, monkeypatch)
    provider = SimpleNamespace(is_available=lambda: False, unavailable_reason=lambda: "")
    monkeypatch.setattr("plugins.memory.load_memory_provider", lambda *a, **kw: provider)
    gateway = runner()
    await check_memory_health(gateway)
    gateway._send_home_channel_message.assert_not_awaited()
    assert not caplog.records


def prepare_startup(gateway, monkeypatch):
    """Keep real post-connect ordering without starting transports or restoring sessions."""
    for name in (
        "_start_post_connect_services", "_await_startup_boot_sends",
        "_recover_pending_shutdown_flush_off_loop", "_await_mcp_discovery",
        "_finish_startup_restore", "_send_session_db_warning_notifications",
    ):
        setattr(gateway, name, AsyncMock())
    gateway._claim_pending_obligations = AsyncMock(return_value=[])
    gateway._resume_pending_candidates_async = AsyncMock(return_value=None)
    gateway._startup_interrupted_note_candidates = Mock(return_value=[])
    gateway._schedule_resume_pending_sessions = Mock()
    gateway._schedule_auto_resume_delegations = Mock()
    gateway._spawn_supervised = Mock()
    gateway._run_process_watcher = AsyncMock()
    monkeypatch.setattr("gateway.run_runtime.recover_gateway_native_sessions", AsyncMock())
    monkeypatch.setattr("gateway.run._restart_notification_pending", lambda: False)
    monkeypatch.setattr("gateway.run._planned_restart_notification_pending", lambda: False)
    monkeypatch.setattr("gateway.zero_cut_standby.is_standby", lambda: False)
    monkeypatch.setattr("gateway.cron_store_notices.install_cron_store_notices", Mock())
    monkeypatch.setattr("tools.process_registry.process_registry.pending_watchers", [{"session_id": "recovered"}])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["config", "provider", "dependencies", "requirement"])
async def test_readiness_failure_is_logged_once_and_startup_checks_other_profiles(
    tmp_path, monkeypatch, caplog, failure,
):
    from gateway.run_startup import GatewayStartupMixin
    from hermes_constants import get_hermes_home
    from packaging.version import Version
    from pm.package import InstallError
    from pm.plugins_state import read_home_selection

    home = setup_home(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    (other / "config.yaml").write_text("memory: {provider: hindsight}\n")
    monkeypatch.setattr("gateway.run._handoff_watch_scopes", lambda _: [("broken", home), ("healthy", other)])
    checked = []

    def selection(selected_home):
        checked.append(selected_home)
        if selected_home == home and failure == "config":
            raise ValueError("invalid memory configuration")
        return read_home_selection(selected_home)

    def reason():
        if failure == "provider":
            Version("broken-install")
        return "Client is unavailable"

    def dependencies(_):
        if failure == "dependencies":
            raise InstallError("hindsight", "missing memory manifest")
        return {"python_dependencies": ["not a requirement !!!"]}, []

    monkeypatch.setattr("pm.plugins_state.read_home_selection", selection)
    monkeypatch.setattr("plugins.memory.load_memory_provider", lambda *a, **kw: SimpleNamespace(
        is_available=lambda: get_hermes_home() == other, unavailable_reason=reason))
    monkeypatch.setattr("hermes_cli.memory_setup.memory_provider_dependency_inputs", dependencies)
    gateway = runner()
    prepare_startup(gateway, monkeypatch)
    caplog.set_level(logging.ERROR, logger="gateway.runtime_health")

    # This awaits install_health_notices before it can resume even one recovered watcher.
    await GatewayStartupMixin._start_finish_wiring(gateway, 0)

    assert checked == [home, other]
    gateway._spawn_supervised.assert_called_once()
    assert gateway._spawn_supervised.call_args.args[1] == "process_watcher:recovered"
    errors = [record for record in caplog.records if record.name == "gateway.runtime_health"]
    assert len(errors) == 1
    assert errors[0].levelno == logging.ERROR
    assert "broken" in errors[0].getMessage() and str(home) in errors[0].getMessage()
    assert errors[0].exc_info is not None
    gateway._send_home_channel_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_step", ["cron", "memory"])
async def test_health_notice_setup_failure_does_not_abort_startup(tmp_path, monkeypatch, caplog, failed_step):
    from gateway.run_startup import GatewayStartupMixin

    setup_home(tmp_path, monkeypatch)
    gateway = runner()
    prepare_startup(gateway, monkeypatch)
    if failed_step == "cron":
        monkeypatch.setattr("gateway.cron_store_notices.install_cron_store_notices",
                            Mock(side_effect=RuntimeError("cron notice setup failed")))
    else:
        monkeypatch.setattr("gateway.runtime_health.check_memory_health",
                            AsyncMock(side_effect=RuntimeError("memory check setup failed")))
    caplog.set_level(logging.ERROR, logger="gateway.runtime_health")

    await GatewayStartupMixin._start_finish_wiring(gateway, 0)

    gateway._spawn_supervised.assert_called_once()
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.ERROR
    assert isinstance(caplog.records[0].exc_info[1], RuntimeError)


@pytest.mark.asyncio
async def test_periodic_failure_is_logged_and_single_flight_reopens(tmp_path, monkeypatch, caplog):
    import asyncio
    from gateway.runtime_health import schedule_memory_health

    setup_home(tmp_path, monkeypatch)
    probe = AsyncMock(side_effect=RuntimeError("periodic readiness failed"))
    monkeypatch.setattr("gateway.runtime_health.check_memory_health", probe)
    gateway = runner()
    caplog.set_level(logging.ERROR, logger="gateway.runtime_health")
    loop = asyncio.get_running_loop()
    schedule_memory_health(gateway, loop)
    failed = gateway._memory_health_future
    schedule_memory_health(gateway, loop)
    assert gateway._memory_health_future is failed
    with pytest.raises(RuntimeError, match="periodic readiness failed"):
        await asyncio.wrap_future(failed)
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.ERROR
    assert "periodic" in caplog.records[0].getMessage().lower()
    assert isinstance(caplog.records[0].exc_info[1], RuntimeError)
    probe.assert_awaited_once()

    probe.side_effect = None
    schedule_memory_health(gateway, loop)
    assert gateway._memory_health_future is not failed
    await asyncio.wrap_future(gateway._memory_health_future)
    assert probe.await_count == 2
    assert len(caplog.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("display", [
    "display: {suppress_warning_notifications: true}\n",
    "display: {platforms: {fixture: {suppress_warning_notifications: true}}}\n",
])
async def test_muted_profile_is_log_only_without_a_delivery_marker(tmp_path, monkeypatch, caplog, display):
    from gateway.runtime_health import _marker, check_memory_health

    home = setup_home(tmp_path, monkeypatch)
    (home / "config.yaml").write_text("memory: {provider: hindsight}\n" + display)
    other = tmp_path / "other"
    other.mkdir()
    (other / "config.yaml").write_text("memory: {provider: hindsight}\n")
    (other / "hindsight").mkdir()
    (other / "hindsight/config.json").write_text(json.dumps({"mode": "local_external"}))
    monkeypatch.setattr("gateway.run._handoff_watch_scopes", lambda _: [(None, home), ("other", other)])
    gateway = runner()
    gateway._served_home_channel_transports = lambda: [
        (None, "fixture", None, "muted", object()),
        ("other", "fixture", None, "visible", object()),
    ]
    caplog.set_level(logging.ERROR, logger="gateway.runtime_health")

    await check_memory_health(gateway)
    await check_memory_health(gateway)

    gateway._send_home_channel_message.assert_awaited_once()
    assert gateway._send_home_channel_message.call_args.args[1] == "visible"
    assert _marker(home).with_suffix(".logged").exists()
    assert not _marker(home).with_suffix(".notified").exists()
    assert _marker(other).with_suffix(".notified").exists()
    assert len(caplog.records) == 2  # one logged diagnostic per profile, not per retry

    # A muted presentation is not delivery. Opting back in can still deliver this release's notice.
    (home / "config.yaml").write_text("memory: {provider: hindsight}\n")
    await check_memory_health(gateway)
    assert gateway._send_home_channel_message.await_count == 2
    assert gateway._send_home_channel_message.call_args.args[1] == "muted"
    assert _marker(home).with_suffix(".notified").exists()
    assert len(caplog.records) == 2
