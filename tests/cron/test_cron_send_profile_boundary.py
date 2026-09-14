"""Exercise the real registry and send engine at the profile/attempt boundary."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cron import outbound
from gateway.config import GatewayConfig, Platform, PlatformConfig
from tools import send_message_tool
from tools.registry import registry


@pytest.fixture
def scoped_send(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "work")
    config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="work-token")})
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    monkeypatch.setattr(send_message_tool, "prepare_send_message_platforms", lambda: None)
    monkeypatch.setattr(send_message_tool, "_mirror_sent_message", lambda *a: False)
    run = outbound.CronMessagingRun("job", "run", {"platform": "telegram", "chat_id": "123", "thread_id": "42"},
                                    True, tmp_path / "cron" / "outbound.db")
    token = outbound._current_run.set(run)
    args = {"message": "hello", "message_key": "notice", "_profile": "default", "account": "default"}
    yield lambda: json.loads(registry.dispatch("send_message", args)), run, config
    outbound._current_run.reset(token)


@pytest.mark.parametrize("platform", [Platform.TELEGRAM, Platform.SLACK, Platform.MATRIX])
def test_bound_profile_uses_its_live_adapter(scoped_send, monkeypatch, platform):
    send, run, config = scoped_send
    run.origin["platform"] = platform.value
    chat_id, thread_id = {
        Platform.TELEGRAM: ("123", "42"),
        Platform.SLACK: ("C0123456789", "1234567890.123456"),
        Platform.MATRIX: ("!room:example.invalid", "$event"),
    }[platform]
    run.origin.update(chat_id=chat_id, thread_id=thread_id)
    config.platforms[platform] = PlatformConfig(enabled=True, token="work-token")
    owner = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True, message_id="owner")))
    default = SimpleNamespace(send=AsyncMock())
    runner = SimpleNamespace(adapters={platform: default},
                             _profile_adapters={"work": {platform: owner}},
                             _primary_profile_name="default", _active_profile_name=lambda: "work", _gateway_loop=None)
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)
    standalone = AsyncMock(side_effect=AssertionError("must not use standalone credentials"))
    monkeypatch.setattr(send_message_tool, "_send_telegram", standalone)
    assert send()["status"] == "verified"
    owner.send.assert_awaited_once_with(chat_id=chat_id, content="hello", metadata={"thread_id": thread_id})
    default.send.assert_not_awaited()
    standalone.assert_not_awaited()
    assert send()["skipped"]


def test_missing_bound_adapter_can_retry_after_repair(scoped_send, monkeypatch):
    send, _, _ = scoped_send
    owner = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True, message_id="owner")))
    runner = SimpleNamespace(adapters={}, _profile_adapters={}, _primary_profile_name="default", _active_profile_name=lambda: "work", _gateway_loop=None)
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)
    assert send()["status"] == "failed"
    runner._profile_adapters["work"] = {Platform.TELEGRAM: owner}
    assert send()["status"] == "verified"
    owner.send.assert_awaited_once()


def test_config_failure_can_retry_but_transport_exception_cannot(scoped_send, monkeypatch):
    send, _, config = scoped_send
    def broken_config():
        raise ValueError("invalid config")
    monkeypatch.setattr("gateway.config.load_gateway_config", broken_config)
    assert send()["status"] == "failed"
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    transport = AsyncMock(side_effect=TimeoutError("response lost"))
    monkeypatch.setattr(send_message_tool, "_send_to_platform", transport)
    assert send()["status"] == "ambiguous"
    assert send()["skipped"]
    transport.assert_awaited_once()


def test_partial_chunk_send_does_not_become_retryable(scoped_send, monkeypatch):
    send, _, _ = scoped_send
    runner = SimpleNamespace(adapters={}, _profile_adapters={}, _primary_profile_name="default", _active_profile_name=lambda: "work", _gateway_loop=None)
    async def first_chunk(**kwargs):
        runner._profile_adapters.clear()
        return SimpleNamespace(success=True, message_id="first")
    owner = SimpleNamespace(send=AsyncMock(side_effect=first_chunk))
    runner._profile_adapters["work"] = {Platform.TELEGRAM: owner}
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)
    monkeypatch.setattr(send_message_tool, "_platform_max_length", lambda _: 3)
    assert send()["status"] == "ambiguous"
    assert send()["skipped"]
    owner.send.assert_awaited_once()


def test_standalone_uses_pinned_home_and_rejects_scope_change(scoped_send, tmp_path, monkeypatch):
    send, _, _ = scoped_send
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: None)
    standalone = AsyncMock(return_value={"success": True, "message_id": "native"})
    monkeypatch.setattr(send_message_tool, "_send_telegram", standalone)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other"))
    assert send()["status"] == "failed"
    standalone.assert_not_awaited()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert send()["status"] == "verified"
    assert standalone.call_args.args[:2] == ("work-token", "123")


@pytest.mark.parametrize("registration", ["own", "global", "other"])
def test_custom_handler_requires_own_scoped_registration(scoped_send, tmp_path, registration):
    from gateway.platform_registry import PlatformEntry, platform_registry

    send, run, config = scoped_send
    name = "cron-profile-handler-test"
    handler = AsyncMock(return_value={"success": True, "message_id": "custom"})
    entry = PlatformEntry(name=name, label="Cron handler test", adapter_factory=lambda _: None,
                          check_fn=lambda: True, send_message_handler=handler)
    own_scope = platform_registry.current_scope_key()
    scope = own_scope if registration == "own" else str(tmp_path / "other")
    # A global fallback must never confer ownership on another profile.
    platform_registry.register(entry)
    if registration != "global":
        platform_registry.register(entry, scope=scope)
    try:
        run.origin.update(platform=name, thread_id=None)
        config.platforms[Platform(name)] = PlatformConfig(enabled=True, token="scoped-token")
        result = send()
        if registration == "own":
            assert result["status"] == "verified"
            handler.assert_awaited_once()
            assert handler.call_args.args[1:3] == ("123", name)
        else:
            assert result["status"] == "failed"
            handler.assert_not_awaited()
            # Repair just the scope, then the same idempotency key can be sent.
            platform_registry.register(entry, scope=own_scope)
            assert send()["status"] == "verified"
            handler.assert_awaited_once()
    finally:
        platform_registry.unregister(name)
        platform_registry.unregister(name, scope=scope)
        platform_registry.unregister(name, scope=own_scope)
