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


def test_real_memory_tool_write_is_journaled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    plugin = load_plugin()
    from tools.memory_tool import MemoryStore, memory_tool
    store = MemoryStore(); store.load_from_disk()
    args = {"action": "add", "target": "memory", "content": "real path"}
    plugin._on_pre_tool_call("memory", args, "real", tool_call_id="real-1")
    result = memory_tool(store=store, **args)
    plugin._on_post_tool_call("memory", args, result, "real", tool_call_id="real-1")
    decoded = json.loads(result)
    assert decoded["success"] is True
    entry = plugin._last()
    assert entry and entry["action"] == "add" and entry["after_hash"] != entry["before_hash"]



def _seed(plugin, path, n, start_at):
    """Write n journaled edits, one per call, with increasing timestamps."""
    for i in range(n):
        plugin._on_pre_tool_call("memory", {"action": "add", "target": "memory"}, "s", tool_call_id=f"c{i}")
        path.write_text(f"v{i}\n", encoding="utf-8")
        import time as _t
        real = _t.time
        _t.time = lambda: start_at + i * 3600  # one entry per hour
        try:
            plugin._on_post_tool_call("memory", {"action": "add", "target": "memory"}, '{"success": true}', "s", tool_call_id=f"c{i}")
        finally:
            _t.time = real


def test_damaged_chain_makes_undo_refuse_and_list_report(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    plugin = load_plugin(); home = tmp_path / ".hermes"; path = home / "memories" / "MEMORY.md"
    path.parent.mkdir(parents=True); path.write_text("a\n")
    _seed(plugin, path, 3, 1_700_000_000)
    ok = json.loads(plugin.memory_journal_list({}))
    assert ok["intact"] is True and ok["entries"] == 3 and ok["recent"][0]["sequence"] == 3
    # Tamper with the middle entry's content: its hash no longer matches.
    lines = plugin._journal().read_text().splitlines()
    row = json.loads(lines[1]); row["after"] = "tampered"; lines[1] = json.dumps(row, sort_keys=True)
    plugin._journal().write_text("\n".join(lines) + "\n")
    report = json.loads(plugin.memory_journal_list({}))
    assert report["intact"] is False and "entry 2" in report["damage"]
    refused = json.loads(plugin.memory_undo({}, task_id="s"))
    assert refused["error_code"] == "journal_damaged"
    # Memory writes still journal-observe without raising (the plugin only observes).
    plugin._on_pre_tool_call("memory", {"action": "add", "target": "memory"}, "s", tool_call_id="after-damage")
    path.write_text("after damage\n")
    plugin._on_post_tool_call("memory", {"action": "add", "target": "memory"}, '{"success": true}', "s", tool_call_id="after-damage")


def test_unparsable_line_is_reported_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    plugin = load_plugin(); home = tmp_path / ".hermes"; path = home / "memories" / "MEMORY.md"
    path.parent.mkdir(parents=True); path.write_text("a\n")
    _seed(plugin, path, 2, 1_700_000_000)
    with plugin._journal().open("a") as fh: fh.write("{not json\n")
    report = json.loads(plugin.memory_journal_list({}))
    assert report["intact"] is False and "not valid JSON" in report["damage"]
    assert json.loads(plugin.memory_undo({}, task_id="s"))["error_code"] == "journal_damaged"


def test_retention_keeps_recent_plus_one_per_day_and_reseals(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    plugin = load_plugin(); home = tmp_path / ".hermes"; path = home / "memories" / "MEMORY.md"
    path.parent.mkdir(parents=True); path.write_text("a\n")
    start = 1_700_000_000
    _seed(plugin, path, 60, start)  # 60 hourly entries = 2.5 days
    now = start + 60 * 3600
    result = plugin.compact(keep_recent=10, keep_days=90, now=now)
    assert result["success"] is True
    rows, damage = plugin._load()
    assert damage is None, damage
    # 10 most recent kept verbatim; older 50 hourly entries collapse to one per day (3 calendar days touched).
    assert [r["sequence"] for r in rows[-10:]] == list(range(51, 61))
    older = rows[:-10]
    assert 1 <= len(older) <= 3
    # Sequence 1 is the first entry of its day, so it survives as the root: no cut before it.
    assert rows[0]["previous_hash"] == "" and rows[0]["sequence"] == 1 and "compacted_from" not in rows[0]
    assert result["dropped"] == 60 - len(rows)
    # Chain is intact after resealing and undo still works on the newest entry.
    assert json.loads(plugin.memory_journal_list({}))["intact"] is True
    assert json.loads(plugin.memory_undo({}, task_id="s"))["success"] is True


def test_entries_older_than_the_window_are_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    plugin = load_plugin(); home = tmp_path / ".hermes"; path = home / "memories" / "MEMORY.md"
    path.parent.mkdir(parents=True); path.write_text("a\n")
    start = 1_700_000_000
    _seed(plugin, path, 5, start)
    far_future = start + 400 * 86400
    result = plugin.compact(keep_recent=2, keep_days=90, now=far_future)
    rows, damage = plugin._load()
    assert damage is None and [r["sequence"] for r in rows] == [4, 5]
    assert result["dropped"] == 3
    # The new root records that entries were cut ahead of it.
    assert rows[0]["compacted_from"] == 1 and rows[0]["previous_hash"] == ""


def test_compaction_refuses_a_damaged_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    plugin = load_plugin(); home = tmp_path / ".hermes"; path = home / "memories" / "MEMORY.md"
    path.parent.mkdir(parents=True); path.write_text("a\n")
    _seed(plugin, path, 3, 1_700_000_000)
    with plugin._journal().open("a") as fh: fh.write("garbage\n")
    before = plugin._journal().read_text()
    assert plugin.compact(keep_recent=1)["error_code"] == "journal_damaged"
    assert plugin._journal().read_text() == before


def test_journal_file_is_owner_only(tmp_path, monkeypatch):
    import stat
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    plugin = load_plugin(); home = tmp_path / ".hermes"; path = home / "memories" / "MEMORY.md"
    path.parent.mkdir(parents=True); path.write_text("a\n")
    _seed(plugin, path, 1, 1_700_000_000)
    assert stat.S_IMODE(plugin._journal().stat().st_mode) == 0o600
