"""The socket's agent-only update command cannot bypass the lifecycle card."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway.update_launcher import make_agent_update_handler, launch_native_update
from tests.gateway.test_agent_update_launcher import _real_store_with_routes


@pytest.fixture
def outside(monkeypatch):
    monkeypatch.setattr("time.time", lambda: datetime.fromisoformat("2026-11-05T14:00:00-08:00").timestamp())
    prompt = Mock(return_value="accept")
    monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", prompt)
    return prompt


def test_agent_socket_update_cannot_bypass_card(tmp_path, monkeypatch, outside):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store, owner, _ = _real_store_with_routes(tmp_path)
    runner = SimpleNamespace(_session_db=SimpleNamespace(_db=store._db),
                             _schedule_update_notification_watch=Mock())
    spawn = Mock()
    handler = make_agent_update_handler(runner=runner, home=tmp_path, main_loop=Mock(),
        resolve_hermes_bin=lambda: ["hermes"], is_managed=lambda: False, spawn=spawn)
    try:
        result = handler({"session_id": owner, "reason": "Apply fix"})
    finally:
        store._db.close()
    assert result["status"] == "approval_required"
    assert result["accepted"] is False
    assert "request_update" in result["error"]
    outside.assert_not_called()
    spawn.assert_not_called()
    assert not (tmp_path / ".update_pending.json").exists()


def test_scheduled_native_update_is_not_gated(tmp_path, outside):
    # Nightly scripts use the native updater, not the agent's registry/socket admission.
    spawn = Mock()
    result = launch_native_update(home=tmp_path, hermes_cmd=["hermes"],
        pending={"reason": "scheduled nightly update"}, spawn=spawn)
    assert result["started"]
    spawn.assert_called_once()
    outside.assert_not_called()


@pytest.mark.parametrize("gateway_mode", [False, True], ids=["human-shell", "scheduled-nightly"])
def test_native_cli_update_is_not_gated(tmp_path, monkeypatch, outside, gateway_mode):
    from hermes_cli import main as cli
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr("hermes_cli.update_owning_install.retarget_to_owning_install", Mock())
    monkeypatch.setattr(cli, "_update_preflight_handled", lambda _: False)
    monkeypatch.setattr(cli, "_install_hangup_protection", Mock())
    monkeypatch.setattr(cli, "_finalize_update_output", Mock())
    monkeypatch.setattr(cli, "_finalize_update_receipt", Mock())
    lock = Mock()
    lock.acquire.return_value = True
    monkeypatch.setattr("hermes_cli.update_lock.UpdateLock", lambda **_: lock)
    execute = Mock()
    monkeypatch.setattr("hermes_cli.update_cmd._cmd_update_impl", execute)
    args = SimpleNamespace(gateway=gateway_mode)
    cli.cmd_update(args)
    execute.assert_called_once_with(args, gateway_mode=gateway_mode)
    outside.assert_not_called()


@pytest.mark.asyncio
async def test_human_slash_restart_is_immediate_outside_window(tmp_path, monkeypatch, outside):
    import gateway.run as gateway_run
    from gateway.platforms.event import MessageEvent, MessageType
    from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, _ = make_restart_runner()
    runner.request_restart = Mock(return_value=True)
    event = MessageEvent(text="/restart", message_type=MessageType.TEXT,
                         source=make_restart_source(), message_id="human-click", platform_update_id=123)
    await runner._handle_restart_command(event)
    runner.request_restart.assert_called_once()
    outside.assert_not_called()
