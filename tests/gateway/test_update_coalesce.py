"""Update requests that arrive close together become one gateway restart.

Incident (2026-10-13): three updates landed within nine minutes and Hermes restarted 19 times that
day, each restart cutting 5-23 running sessions. A request that arrives while an install is in flight,
or within ``updates.coalesce_window_seconds`` of the last launch, is deferred into
``.update_deferred.json`` (latest target wins). Gateway housekeeping launches it once, when no
install is in flight and the window has passed. A lone request still launches immediately. The
deferred launch is an ordinary ``hermes update``, so the live-commit guard still applies.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from gateway import update_launcher as launcher
from tests.gateway.update_fixtures import finalize_update


def _request(reason: str, **extra) -> dict:
    return {"platform": "telegram", "chat_id": "42", "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(), **extra}


def _launch(home, reason, *, now, window=120, spawn=None, **extra):
    return launcher.launch_native_update(
        home=home, hermes_cmd=["hermes"], pending=_request(reason, **extra),
        spawn=spawn or Mock(), coalesce_window=window, now=now)


def test_lone_request_launches_immediately(tmp_path):
    spawn = Mock()
    assert _launch(tmp_path, "first", now=1000.0, spawn=spawn) == {"started": True, "pending": False}
    spawn.assert_called_once()


def test_request_during_inflight_install_is_deferred_latest_wins(tmp_path):
    _launch(tmp_path, "first", now=1000.0)
    second = _launch(tmp_path, "second", now=1030.0, branch="fix/a")
    third = _launch(tmp_path, "third", now=1060.0, branch="fix/b")
    assert second["started"] is False and second["coalesced"] is True
    assert third["coalesced"] is True and third["due_at"] == 1120.0
    deferred = json.loads((tmp_path / launcher.DEFERRED_FILE).read_text(encoding="utf-8"))
    assert deferred["request"]["reason"] == "third"
    assert deferred["request"]["branch"] == "fix/b"
    assert deferred["merged"] == 2
    # The in-flight request's marker is untouched.
    assert json.loads((tmp_path / ".update_pending.json").read_text(encoding="utf-8"))["reason"] == "first"


def test_duplicate_of_inflight_request_is_not_queued_again(tmp_path):
    _launch(tmp_path, "same", now=1000.0)
    assert _launch(tmp_path, "same", now=1010.0) == {"started": False, "pending": True}
    assert not (tmp_path / launcher.DEFERRED_FILE).exists()


def test_request_within_window_after_finished_install_is_deferred(tmp_path):
    _launch(tmp_path, "first", now=1000.0)
    finalize_update(tmp_path)
    spawn = Mock()
    result = _launch(tmp_path, "second", now=1050.0, spawn=spawn)
    assert result["coalesced"] is True and result["due_at"] == 1120.0
    spawn.assert_not_called()


def test_request_after_window_launches_immediately(tmp_path):
    _launch(tmp_path, "first", now=1000.0)
    finalize_update(tmp_path)
    spawn = Mock()
    assert _launch(tmp_path, "second", now=1121.0, spawn=spawn)["started"] is True
    spawn.assert_called_once()


def test_zero_window_keeps_the_old_refusal(tmp_path):
    _launch(tmp_path, "first", now=1000.0, window=0)
    assert _launch(tmp_path, "second", now=1001.0, window=0) == {"started": False, "pending": True}
    assert not (tmp_path / launcher.DEFERRED_FILE).exists()


def test_due_deferred_request_launches_once_with_its_branch(tmp_path):
    _launch(tmp_path, "first", now=1000.0)
    _launch(tmp_path, "second", now=1010.0, branch="fix/a")
    _launch(tmp_path, "third", now=1020.0, branch="fix/b")
    spawn = Mock()
    # Still in flight: nothing launches.
    assert launcher.launch_deferred_update(home=tmp_path, hermes_cmd=["hermes"], spawn=spawn, now=2000.0) is None
    finalize_update(tmp_path)
    # Finished but the window since the last launch has not passed.
    assert launcher.launch_deferred_update(home=tmp_path, hermes_cmd=["hermes"], spawn=spawn, now=1100.0) is None
    result = launcher.launch_deferred_update(home=tmp_path, hermes_cmd=["hermes"], spawn=spawn, now=1121.0)
    assert result == {"started": True, "pending": False}
    spawn.assert_called_once()
    assert spawn.call_args.kwargs["update_args"] == ["--branch", "fix/b"]
    marker = json.loads((tmp_path / ".update_pending.json").read_text(encoding="utf-8"))
    assert marker["reason"] == "third" and marker["branch"] == "fix/b" and marker["coalesced_requests"] == 3
    assert not (tmp_path / launcher.DEFERRED_FILE).exists()
    assert launcher.launch_deferred_update(home=tmp_path, hermes_cmd=["hermes"], spawn=spawn, now=5000.0) is None
    spawn.assert_called_once()


def test_deferred_main_request_passes_no_branch(tmp_path):
    _launch(tmp_path, "first", now=1000.0)
    _launch(tmp_path, "second", now=1010.0)
    finalize_update(tmp_path)
    spawn = Mock()
    launcher.launch_deferred_update(home=tmp_path, hermes_cmd=["hermes"], spawn=spawn, now=1200.0)
    assert spawn.call_args.kwargs["update_args"] == []


def test_failed_deferred_launch_keeps_the_request(tmp_path):
    _launch(tmp_path, "first", now=1000.0)
    _launch(tmp_path, "second", now=1010.0)
    finalize_update(tmp_path)
    with pytest.raises(OSError):
        launcher.launch_deferred_update(home=tmp_path, hermes_cmd=["hermes"],
                                        spawn=Mock(side_effect=OSError("no")), now=1200.0)
    assert json.loads((tmp_path / launcher.DEFERRED_FILE).read_text(encoding="utf-8"))["request"]["reason"] == "second"


def test_pending_or_deferred_update_absorbs_a_restart(tmp_path):
    assert launcher.restart_absorbed_by_update(tmp_path) is None
    _launch(tmp_path, "first", now=1000.0)
    assert "in progress" in launcher.restart_absorbed_by_update(tmp_path)
    _launch(tmp_path, "second", now=1010.0)
    finalize_update(tmp_path)
    assert "queued" in launcher.restart_absorbed_by_update(tmp_path)


def test_stale_inflight_marker_never_blocks_restart(tmp_path):
    stale = _request("dead updater")
    stale["timestamp"] = datetime.fromtimestamp(0, timezone.utc).isoformat()
    launcher.launch_native_update(home=tmp_path, hermes_cmd=["hermes"], pending=stale, spawn=Mock(),
                                  coalesce_window=0)
    assert launcher.restart_absorbed_by_update(tmp_path) is None


def test_window_config_default_and_override(monkeypatch):
    from hermes_cli import config as config_module
    monkeypatch.setattr(config_module, "load_config", lambda: {})
    assert launcher.coalesce_window_seconds() == 120.0
    monkeypatch.setattr(config_module, "load_config", lambda: {"updates": {"coalesce_window_seconds": 0}})
    assert launcher.coalesce_window_seconds() == 0.0
    monkeypatch.setattr(config_module, "load_config", lambda: {"updates": {"coalesce_window_seconds": "x"}})
    assert launcher.coalesce_window_seconds() == 120.0


def test_window_config_is_validated():
    from hermes_cli.config import validate_config_structure
    issues = validate_config_structure({"updates": {"coalesce_window_seconds": -5}})
    assert any("updates.coalesce_window_seconds" in issue.message for issue in issues)
    assert not [i for i in validate_config_structure({"updates": {"coalesce_window_seconds": 0}})
                if "coalesce_window_seconds" in i.message]


def test_spawn_accepts_update_args(tmp_path, monkeypatch):
    from gateway import slash_commands
    calls = []
    monkeypatch.setattr(slash_commands.subprocess if hasattr(slash_commands, "subprocess") else __import__("subprocess"),
                        "Popen", lambda argv, **_k: calls.append(argv))
    monkeypatch.setattr("sys.platform", "darwin")
    slash_commands._spawn_detached_update(["hermes"], tmp_path / "out", tmp_path / "exit",
                                          update_args=["--branch", "fix/x y"])
    script = calls[0][-1]
    assert "hermes update --gateway --branch 'fix/x y'" in script


def test_admission_primitive_without_a_window_keeps_the_exclusive_refusal(tmp_path, monkeypatch):
    """Coalescing is opt-in per caller; the bare primitive never defers even if config enables it."""
    monkeypatch.setattr(launcher, "coalesce_window_seconds", lambda: 120.0)
    spawn = Mock()
    launcher.launch_native_update(home=tmp_path, hermes_cmd=["hermes"], pending=_request("first"), spawn=spawn)
    assert launcher.launch_native_update(home=tmp_path, hermes_cmd=["hermes"], pending=_request("second"),
                                         spawn=spawn) == {"started": False, "pending": True}
    spawn.assert_called_once()
    assert not (tmp_path / launcher.DEFERRED_FILE).exists()


@pytest.mark.asyncio
async def test_slash_update_opts_into_the_configured_window(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    runner = object.__new__(GatewayRunner)
    runner.adapters, runner._voice_mode, runner._update_prompt_pending = {}, {}, {}
    runner._schedule_update_notification_watch = MagicMock()
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (root / "gateway").mkdir()
    (root / "gateway" / "run.py").touch()
    home = tmp_path / "home"
    home.mkdir()
    launch = Mock(return_value={"started": False, "pending": True, "coalesced": True, "merged": 1, "due_at": 1.0})
    monkeypatch.setattr(launcher, "coalesce_window_seconds", lambda: 77.0)
    monkeypatch.setattr(launcher, "launch_native_update", launch)
    event = MessageEvent(text="/update", source=SessionSource(
        platform=Platform.TELEGRAM, user_id="1", chat_id="2", user_name="u"))
    with patch("gateway.run._hermes_home", home), patch("gateway.run.__file__", str(root / "gateway" / "run.py")), \
         patch("hermes_cli.config.detect_install_method", return_value="git"), \
         patch("shutil.which", return_value="/usr/bin/hermes"):
        await runner._handle_update_command(event)
    assert launch.call_args.kwargs["coalesce_window"] == 77.0
    runner._schedule_update_notification_watch.assert_called_once()
