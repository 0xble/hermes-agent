from __future__ import annotations

import json


def test_request_update_refuses_delegated_child(monkeypatch):
    import sys
    sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))
    import importlib.util
    spec = importlib.util.spec_from_file_location("request_update", __import__('pathlib').Path(__file__).parent / "__init__.py")
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    monkeypatch.setattr(plugin, "_is_child", lambda: True)
    result = json.loads(plugin.request_update({"reason": "test"}))
    assert result["error_code"] == "parent_only"


def test_request_update_checks_then_spawns(monkeypatch, tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("request_update", Path(__file__).parent / "__init__.py")
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    home = tmp_path
    pending = home / ".update_pending.json"
    monkeypatch.setattr(plugin, "_home", lambda: home)
    monkeypatch.setattr(plugin, "_is_child", lambda: False)

    class Result:
        returncode = 0
        stdout = "Updating abc..def"
        stderr = ""

    calls = []
    monkeypatch.setattr(plugin.subprocess, "run", lambda *a, **k: (calls.append((a, k)) or Result()))
    spawned = []
    import gateway.run as gateway_run
    import gateway.slash_commands as slash_commands
    monkeypatch.setattr(gateway_run, "_resolve_hermes_bin", lambda: ["hermes"])
    monkeypatch.setattr(slash_commands, "_spawn_detached_update", lambda *a: spawned.append(a))

    for var in [k for k in list(__import__("os").environ) if k.startswith("HERMES_SESSION_")]:
        monkeypatch.delenv(var, raising=False)  # a gateway-launched shell would otherwise route the marker
    result = json.loads(plugin.request_update({"reason": "candidate patch"}))
    assert result == {"reason": "candidate patch", "routed": False, "status": "accepted", "success": True, "watcher": "no_gateway"}
    assert json.loads(pending.read_text())["reason"] == "candidate patch"
    assert spawned and spawned[0][0] == ["hermes"]
    assert len(calls) == 1


def test_request_update_arms_the_running_gateways_watcher(monkeypatch, tmp_path):
    """Inside a gateway, the tool must arm the same completion watcher the /update command does."""
    import importlib.util, sys
    from pathlib import Path
    from types import ModuleType, SimpleNamespace
    spec = importlib.util.spec_from_file_location("request_update", Path(__file__).parent / "__init__.py")
    plugin = importlib.util.module_from_spec(spec); spec.loader.exec_module(plugin)
    armed = []
    runner = SimpleNamespace(_schedule_update_notification_watch=lambda: armed.append(True))
    fake_run = ModuleType("gateway.run"); fake_run._gateway_runner_ref = lambda: runner
    monkeypatch.setitem(sys.modules, "gateway.run", fake_run)
    # Simulate being called from the event-loop thread.
    import asyncio
    async def go():
        return plugin._arm_update_watcher()
    assert asyncio.run(go()) == "armed"
    assert armed == [True]
