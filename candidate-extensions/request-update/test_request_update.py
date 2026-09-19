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
    pending = tmp_path / ".update_pending.json"
    monkeypatch.setattr(plugin, "_pending_path", lambda: pending)
    monkeypatch.setattr(plugin, "_is_child", lambda: False)

    class Result:
        returncode = 0
        stdout = "Updating abc..def"
        stderr = ""

    calls = []
    monkeypatch.setattr(plugin.subprocess, "run", lambda *a, **k: (calls.append((a, k)) or Result()))
    monkeypatch.setattr(plugin.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 42})())

    result = json.loads(plugin.request_update({"reason": "candidate patch"}))
    assert result == {"pid": 42, "reason": "candidate patch", "status": "accepted", "success": True}
    assert json.loads(pending.read_text())["reason"] == "candidate patch"
    assert len(calls) == 1
