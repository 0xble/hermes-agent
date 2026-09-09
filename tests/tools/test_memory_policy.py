"""Contract tests for the bounded unattended-memory policy slice."""
import json

from tools import memory_tool as mt
from tools.skill_provenance import reset_current_write_origin, set_current_write_origin


def _review():
    return set_current_write_origin("background_review")


def _configure(tmp_path, monkeypatch, policy, approval=False):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        f"memory:\n  background_policy: {policy}\n  write_approval: {str(approval).lower()}\n", encoding="utf-8")
    store = mt.MemoryStore(); store.load_from_disk()
    assert store.add("memory", "old fact")["success"]
    return store


def test_invalid_batch_is_never_staged(tmp_path, monkeypatch):
    from tools import write_approval as wa
    store = _configure(tmp_path, monkeypatch, "approve_changes")
    token = _review()
    try:
        result = json.loads(mt.memory_tool(operations=[
            {"action": "add", "content": "would be atomic"},
            {"action": "replace", "old_text": "missing", "content": "new"},
        ], store=store))
    finally:
        reset_current_write_origin(token)
    assert not result["success"]
    assert not wa.list_pending(wa.MEMORY)
    assert store.memory_entries == ["old fact"]


def test_approve_changes_stages_mixed_batch_but_permits_add(tmp_path, monkeypatch):
    from tools import write_approval as wa
    store = _configure(tmp_path, monkeypatch, "approve_changes")
    token = _review()
    try:
        add = json.loads(mt.memory_tool("add", content="new fact", store=store))
        mixed = json.loads(mt.memory_tool(operations=[
            {"action": "remove", "old_text": "old fact"},
            {"action": "add", "content": "replacement"},
        ], store=store))
    finally:
        reset_current_write_origin(token)
    assert add["success"] and "new fact" in store.memory_entries
    assert mixed["staged"] and "old fact" in store.memory_entries
    assert "replacement" not in store.memory_entries
    assert wa.get_pending(wa.MEMORY, mixed["pending_id"])["payload"]["action"] == "batch"


def test_automatic_history_recovery_and_conflict_safe_idempotent_rollback(tmp_path, monkeypatch):
    from tools.memory_history import list_history, rollback
    store = _configure(tmp_path, monkeypatch, "automatic")
    token = _review()
    try:
        result = json.loads(mt.memory_tool("replace", content="new fact", old_text="old fact", store=store))
    finally:
        reset_current_write_origin(token)
    assert result["success"] and result["history_id"]
    record = next(row for row in list_history() if row["id"] == result["history_id"])
    assert record["status"] == "applied"
    # A concurrent newer writer must make rollback refuse rather than clobber it.
    assert store.replace("memory", "new fact", "newer fact")["success"]
    assert rollback(record["id"], store)["conflict"] is True
    assert store.replace("memory", "newer fact", "new fact")["success"]
    assert rollback(record["id"], store)["success"]
    assert rollback(record["id"], store)["success"]
    assert store.memory_entries == ["old fact"]


def test_automatic_single_remove_keeps_explicit_delete_semantics(tmp_path, monkeypatch):
    from tools.memory_history import rollback
    store = _configure(tmp_path, monkeypatch, "automatic")
    token = _review()
    try:
        result = json.loads(mt.memory_tool("remove", old_text="old fact", store=store))
    finally:
        reset_current_write_origin(token)
    assert result["success"] and store.memory_entries == []
    assert rollback(result["history_id"], store)["success"]
    assert store.memory_entries == ["old fact"]


def test_general_write_approval_overrides_automatic(tmp_path, monkeypatch):
    from tools import write_approval as wa
    store = _configure(tmp_path, monkeypatch, "automatic", approval=True)
    token = _review()
    try:
        result = json.loads(mt.memory_tool("replace", content="new fact", old_text="old fact", store=store))
    finally:
        reset_current_write_origin(token)
    assert result["staged"]
    assert store.memory_entries == ["old fact"]
    assert wa.get_pending(wa.MEMORY, result["pending_id"])


def test_interrupted_history_is_recovered_without_guessing(tmp_path, monkeypatch):
    from tools.memory_history import HistoryTransaction, list_history, rollback
    store = _configure(tmp_path, monkeypatch, "automatic")
    def interrupted(self):
        raise OSError("simulated history status failure")
    monkeypatch.setattr(HistoryTransaction, "applied", interrupted)
    token = _review()
    try:
        result = json.loads(mt.memory_tool("replace", content="after crash", old_text="old fact", store=store))
    finally:
        reset_current_write_origin(token)
    assert result["success"]
    record = list_history()[0]
    assert record["status"] == "applied" and record["recovered_at"]
    assert rollback(record["id"], store)["success"]
    assert store.memory_entries == ["old fact"]
    never_applied = HistoryTransaction("memory", [])
    never_applied.prepare(store._path_for("memory"), "old fact", ["never written"])
    assert list_history()[0]["status"] == "not_applied"
    assert rollback(never_applied.record["id"], store)["success"]


def test_malformed_batches_and_unknown_policy_fail_closed(tmp_path, monkeypatch):
    from tools import write_approval as wa
    store = _configure(tmp_path, monkeypatch, "unknown")
    token = _review()
    try:
        for ops in ([1], [{"action": "add", "content": 4}], []):
            assert not json.loads(mt.memory_tool(operations=ops, store=store))["success"]
        assert not wa.list_pending(wa.MEMORY)
        result = json.loads(mt.memory_tool("remove", old_text="old fact", store=store))
    finally:
        reset_current_write_origin(token)
    assert result["staged"] and store.memory_entries == ["old fact"]
