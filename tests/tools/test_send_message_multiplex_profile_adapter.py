"""send_message must deliver through the ACTIVE PROFILE's gateway adapter under multiplex.

``runner.adapters`` holds the default profile's bots; a secondary profile's turn that resolved the
live adapter by bare platform posted (and reacted) with the default bot's identity.  The lookup must
honour ``_profile_adapters[profile]`` and fail closed (``None`` → scoped standalone sender / error)
when the profile has no adapter for that platform — never the default bot.
"""
import asyncio
from pathlib import Path

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner, _profile_runtime_scope
from tools.send_message_senders import _live_adapter


@pytest.fixture
def mux_runner(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "profiles" / "sec").mkdir(parents=True)
    (home / "profiles" / "nobot").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    default_slack, sec_slack = object(), object()
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.SLACK: default_slack}
    runner._profile_adapters = {"sec": {Platform.SLACK: sec_slack}, "nobot": {}}
    runner._primary_profile_name = "default"
    import gateway.run as gateway_run
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)
    return home, default_slack, sec_slack


def test_secondary_profile_turn_resolves_its_own_adapter(mux_runner):
    home, default_slack, sec_slack = mux_runner
    with _profile_runtime_scope(home / "profiles" / "sec", {}):
        _, adapter = _live_adapter(Platform.SLACK)
    assert adapter is sec_slack
    _, adapter = _live_adapter(Platform.SLACK)  # default scope still gets the default bot
    assert adapter is default_slack


def test_profile_without_adapter_fails_closed_never_default_bot(mux_runner):
    home, default_slack, _ = mux_runner
    with _profile_runtime_scope(home / "profiles" / "nobot", {}):
        _, adapter = _live_adapter(Platform.SLACK)
    assert adapter is None


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['matching', 'not_loaded', 'wrong_profile', 'live_missing', 'lookup_error'])
async def test_trusted_standalone_native_sender_preserves_profile_and_receipt(tmp_path, monkeypatch, mode):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import gateway.run as gateway_run
    from gateway.platforms import weixin
    from gateway.config import PlatformConfig
    from tools.send_message_tool import _send_to_platform

    home = tmp_path / '.hermes'
    secondary = home / 'profiles' / 'sec'
    secondary.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    native = AsyncMock(return_value={'success': True, 'message_id': 'native-receipt'})
    monkeypatch.setattr(weixin, 'send_weixin_direct', native)
    default_adapter = SimpleNamespace(send=AsyncMock())
    if mode == 'live_missing':
        runner = SimpleNamespace(_primary_profile_name='default', _active_profile_name=lambda: 'default',
            adapters={Platform.WEIXIN: default_adapter}, _profile_adapters={})
        monkeypatch.setattr(gateway_run, '_gateway_runner_ref', lambda: runner)
    elif mode == 'not_loaded':
        import sys
        monkeypatch.delitem(sys.modules, 'gateway.run')
    elif mode == 'lookup_error':
        def unavailable():
            raise RuntimeError('cannot inspect runner')
        monkeypatch.setattr(gateway_run, '_gateway_runner_ref', unavailable)
    else:
        monkeypatch.setattr(gateway_run, '_gateway_runner_ref', lambda: None)
    config = PlatformConfig(enabled=True, token='synthetic-secondary-token', extra={'base_url': 'https://example.invalid'})
    with _profile_runtime_scope(secondary, {}):
        result = await _send_to_platform(Platform.WEIXIN, config, 'chat', 'body',
            media_files=[('/synthetic/document.pdf', False)],
            profile='default' if mode == 'wrong_profile' else 'sec')
    if mode in {'matching', 'not_loaded'}:
        assert result == {'success': True, 'message_id': 'native-receipt'}
        native.assert_awaited_once_with(extra=config.extra, token=config.token,
            chat_id='chat', message='body', media_files=[('/synthetic/document.pdf', False)])
    else:
        assert result.get('error')
        if mode in {'wrong_profile', 'live_missing', 'lookup_error'}:
            assert result.get('delivery_stage') == 'pre_send'
        native.assert_not_awaited()
    default_adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_trusted_send_uses_factory_home_and_launch_owner(mux_runner, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from gateway.platforms.base import SendResult
    from tools.send_message_tool import _send_via_adapter
    import gateway.run as gateway_run

    home, _, _ = mux_runner
    runner = gateway_run._gateway_runner_ref()
    monkeypatch.setattr(runner, "_instantiate_adapter", lambda *a: SimpleNamespace(
        send=AsyncMock(return_value=SendResult(success=True, message_id="receipt"))))
    primary = runner._create_adapter(Platform.SLACK, SimpleNamespace())
    with _profile_runtime_scope(home / "profiles" / "sec", {}):
        secondary = runner._create_adapter(Platform.SLACK, SimpleNamespace())
    runner.adapters = {Platform.SLACK: primary}
    runner._profile_adapters = {"sec": {Platform.SLACK: secondary}}
    runner._gateway_loop = asyncio.get_running_loop()
    with _profile_runtime_scope(home / "profiles" / "sec", {}):
        assert runner._active_profile_name() == "sec"
        result = await _send_via_adapter(Platform.SLACK, SimpleNamespace(), "C123", "hello", profile="sec")
    assert result["success"]
    assert primary._hermes_profile_home == home.resolve()
    assert secondary._hermes_profile_home == (home / "profiles" / "sec").resolve()
    primary.send.assert_not_awaited()
    secondary.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_known", [True, False])
async def test_custom_profile_names_do_not_authorize_other_homes(mux_runner, monkeypatch, owner_known):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from gateway.platforms.base import SendResult
    from tools.send_message_tool import _send_via_adapter
    import gateway.run as gateway_run

    home, _, _ = mux_runner
    runner = gateway_run._gateway_runner_ref()
    runner._primary_profile_name = "custom"
    runner._profile_adapters = {}
    runner._gateway_loop = asyncio.get_running_loop()
    monkeypatch.setattr(runner, "_instantiate_adapter", lambda *a: SimpleNamespace(
        send=AsyncMock(return_value=SendResult(success=True, message_id="receipt"))))
    with _profile_runtime_scope(home.parent / "custom-a", {}):
        wrong = runner._create_adapter(Platform.SLACK, SimpleNamespace())
    if not owner_known:
        del wrong._hermes_profile_home
    runner.adapters = {Platform.SLACK: wrong}
    with _profile_runtime_scope(home.parent / "custom-b", {}):
        assert runner._active_profile_name() == "custom"
        result = await _send_via_adapter(Platform.SLACK, SimpleNamespace(), "C123", "hello", profile="custom")
        assert result["delivery_stage"] == "pre_send"
        wrong.send.assert_not_awaited()
        right = runner._create_adapter(Platform.SLACK, SimpleNamespace())
        runner.adapters[Platform.SLACK] = right
        result = await _send_via_adapter(Platform.SLACK, SimpleNamespace(), "C123", "hello", profile="custom")
    assert result["success"]
    right.send.assert_awaited_once()
