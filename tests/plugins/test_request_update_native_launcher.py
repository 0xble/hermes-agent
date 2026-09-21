"""The installed plugin shares the gateway's update admission and completion protocol."""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("case", ["success", "failed_exit", "spawn_failure", "claimed_race"])
def test_request_update_uses_native_lifecycle(tmp_path, monkeypatch, case):
    import gateway.run
    import gateway.slash_commands
    from gateway.update_notifications import final_outcome

    source = Path(__file__).resolve().parents[2] / "candidate-extensions/request-update/__init__.py"
    spec = importlib.util.spec_from_file_location("request_update_plugin", source)
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    monkeypatch.setattr(plugin, "_home", lambda: tmp_path)
    monkeypatch.setattr(plugin, "_is_child", lambda: False)
    monkeypatch.setattr(plugin, "_arm_update_watcher", lambda: "armed")
    monkeypatch.setattr(gateway.run, "_resolve_hermes_bin", lambda: ["hermes"])
    claimed = tmp_path / ".update_pending.claimed.json"
    def check(*args, **kwargs):
        if case == "claimed_race":
            claimed.write_text('{"reason":"another accepted request"}')
        return SimpleNamespace(returncode=0, stdout="Update available", stderr="")
    monkeypatch.setattr(plugin.subprocess, "run", check)
    spawned = []
    def spawn(command, output, exit_code):
        spawned.append(command)
        if case == "spawn_failure":
            raise RuntimeError("spawn unavailable")
        (tmp_path / ".update_process_exit_code").write_text("1" if case == "failed_exit" else "0")
    monkeypatch.setattr(gateway.slash_commands, "_spawn_detached_update", spawn)
    result = json.loads(plugin.request_update({"reason": "upgrade for regression"}, platform="telegram", chat_id="123"))
    if case == "claimed_race":
        assert result["error_code"] == "update_pending"
        assert not spawned
        assert json.loads(claimed.read_text())["reason"] == "another accepted request"
        assert not (tmp_path / ".update_pending.json").exists()
        return
    if case == "spawn_failure":
        assert result["error_code"] == "spawn_failed"
        assert not (tmp_path / ".update_pending.json").exists()
        return
    assert result["success"] and result["watcher"] == "armed"
    pending = json.loads((tmp_path / ".update_pending.json").read_text())
    assert pending["notification_version"] == 2
    assert pending["chat_id"] == "123" and pending["reason"] == "upgrade for regression"
    assert not (tmp_path / ".update_exit_code").exists()
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    receipt = tmp_path / "logs/update_receipts/latest.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"started_at": now.isoformat(), "finished_at": now.isoformat(),
        "outcome": "success", "post_update": {"sha": "abc123"},
        "fleet": [{"state": "current", "code_sha": "abc123"}]}))
    assert final_outcome(tmp_path, pending)[0] is (case == "success")
