"""Config errors name the repair, not an unrelated login operation."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_cli.model_presets import ModelPresetError


@pytest.mark.parametrize("platform_name", ["TELEGRAM", "SLACK", "LOCAL"])
def test_resolution_reply_classifies_chained_config_error(platform_name):
    from gateway.config import Platform
    from gateway.run_turn_runner import _resolution_failure_reply

    inner = ModelPresetError("delegation: preset cannot be combined with inline model")
    outer = RuntimeError("runtime resolution failed")
    outer.__cause__ = inner
    text, reason, retryable = _resolution_failure_reply(outer, getattr(Platform, platform_name))
    assert text == "Hermes config is invalid; fix config.yaml"
    assert reason == "config_error"
    assert retryable is False
    assert "/login" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["GatewayTurnMixin", "GatewayTurnPersistenceMixin"])
async def test_real_turn_error_notice_and_logging_once_per_config_mtime(tmp_path, monkeypatch, caplog, owner):
    from gateway.run_turn import GatewayTurnMixin
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text("delegation: {model_preset: missing}\n")
    runner = SimpleNamespace(_hmwa_stop_typing_for_turn=AsyncMock())
    prepared = SimpleNamespace(message_text=None, history=[])
    handler = {"GatewayTurnMixin": GatewayTurnMixin, "GatewayTurnPersistenceMixin": GatewayTurnPersistenceMixin}[owner]._hmwa_agent_error_reply
    error = ModelPresetError("model_presets: private/path authored-value sk-synthetic-only")
    with caplog.at_level("ERROR"):
        for _ in range(3):
            reply = await handler(runner, error, None, None, None, "fixture", prepared)
            assert str(reply) == "Hermes config is invalid; fix config.yaml"
            assert "/login" not in str(reply)
        assert sum("Hermes config is invalid" in r.message for r in caplog.records) == 1
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        await handler(runner, error, None, None, None, "fixture", prepared)
    assert sum("Hermes config is invalid" in r.message for r in caplog.records) == 2
    assert any(str(error) in r.message for r in caplog.records)
    assert not any("Agent error in session" in r.message for r in caplog.records)
