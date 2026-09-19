import importlib.util
import json
from pathlib import Path


def load_plugin():
    path = Path(__file__).with_name("__init__.py")
    spec = importlib.util.spec_from_file_location("memory_journal_test_plugin", path)
    module = importlib.util.module_from_spec(spec); assert spec.loader is not None
    spec.loader.exec_module(module); return module


def test_add_replace_remove_and_undo_with_hash_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    plugin = load_plugin(); home = tmp_path / ".hermes"; path = home / "memories" / "MEMORY.md"
    path.parent.mkdir(parents=True); path.write_text("old\n", encoding="utf-8")
    plugin._on_pre_tool_call("memory", {"action": "add", "target": "memory", "content": "new"}, "s", tool_call_id="1")
    path.write_text("old\n§\nnew\n", encoding="utf-8")
    plugin._on_post_tool_call("memory", {"action": "add", "target": "memory"}, '{"success": true}', "s", tool_call_id="1")
    first = plugin._last(); assert first and first["sequence"] == 1 and first["previous_hash"] == ""
    plugin._on_pre_tool_call("memory", {"action": "replace", "target": "memory", "old_text": "new"}, "s", tool_call_id="2")
    path.write_text("old\n§\nrevised\n", encoding="utf-8")
    plugin._on_post_tool_call("memory", {"action": "replace", "target": "memory"}, '{"success": true}', "s", tool_call_id="2")
    second = plugin._last(); assert second["previous_hash"] == first["hash"]
    result = json.loads(plugin.memory_undo({}, task_id="s")); assert result["success"]
    assert path.read_text(encoding="utf-8") == "old\n§\nnew\n"
    path.write_text(path.read_text() + "drift", encoding="utf-8")
    stale = json.loads(plugin.memory_undo({}, task_id="s")); assert stale["error_code"] == "stale_undo"


def test_failed_write_is_not_journaled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    plugin = load_plugin(); plugin._on_pre_tool_call("memory", {"action": "add", "target": "memory"}, "s", tool_call_id="1")
    plugin._on_post_tool_call("memory", {"action": "add", "target": "memory"}, '{"success": false}', "s", tool_call_id="1")
    assert plugin._last() is None

