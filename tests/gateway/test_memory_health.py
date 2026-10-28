"""Boot and periodic memory checks share provider readiness and durable release dedupe."""
import importlib.metadata
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
