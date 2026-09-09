"""Opt-in review policies at real tool/storage boundaries, isolated by HERMES_HOME."""
import json
from contextlib import contextmanager

import pytest

from tools import memory_tool as mt, skill_provenance as provenance
from tools.registry import registry


@contextmanager
def review():
    token = provenance.set_current_write_origin(provenance.BACKGROUND_REVIEW)
    try:
        yield
    finally:
        provenance.reset_current_write_origin(token)


def configure(tmp_path, monkeypatch, policy, approval=False, skill_mode="direct"):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        f"memory:\n  background_policy: {policy}\n  write_approval: {str(approval).lower()}\n"
        f"auxiliary:\n  background_review:\n    skill_mode: {skill_mode}\n", encoding="utf-8")
    store = mt.MemoryStore()
    store.load_from_disk()
    store.add("memory", "Old convention")
    store.add("user", "Old preference")
    return store


@pytest.mark.parametrize("target,old", [("memory", "Old convention"), ("user", "Old preference")])
def test_background_memory_policies_validate_stage_observe_and_rollback(tmp_path, monkeypatch, target, old):
    from tools.memory_history import list_history, rollback
    from tools.review_observations import list_observations
    from tools import write_approval as wa
    store = configure(tmp_path, monkeypatch, "automatic")
    with review():
        result = json.loads(mt.memory_tool("replace", target, "New convention", old, store=store))
    assert result["success"] and not result.get("staged")
    history = list_history()
    assert len(history) == 1 and history[0]["target"] == target
    assert rollback(history[0]["id"], store)["success"]
    assert store._path_for(target).read_text(encoding="utf-8") == old
    assert rollback(history[0]["id"], store)["success"]

    configure(tmp_path, monkeypatch, "approve_changes")
    with review():
        invalid = json.loads(mt.memory_tool("replace", target, "New", "absent", store=store))
        assert not invalid["success"] and not wa.list_pending(wa.MEMORY)
        staged = json.loads(mt.memory_tool("replace", target, "New", old, store=store))
    assert staged["staged"] and store._path_for(target).read_text(encoding="utf-8") == old
    pending_ids = [r["id"] for r in wa.list_pending(wa.MEMORY)]

    configure(tmp_path, monkeypatch, "observe_only")
    with review():
        observed = json.loads(mt.memory_tool("add", target, "Another fact", store=store))
    assert observed["observed"] and store._path_for(target).read_text(encoding="utf-8") == old
    assert len(list_observations(kind="memory")) == 1
    assert [r["id"] for r in wa.list_pending(wa.MEMORY)] == pending_ids


def test_skill_observation_is_idempotent_private_and_never_mutates(tmp_path, monkeypatch):
    from tools.skill_manager_tool import skill_manage
    from tools.review_observations import bind_review_source, list_observations, dispose
    configure(tmp_path, monkeypatch, "approve_changes", skill_mode="observe")
    external = tmp_path / "external"; external.mkdir()
    skill = external / "SKILL.md"; skill.write_text("original", encoding="utf-8")
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "example").symlink_to(external, target_is_directory=True)
    with review(), bind_review_source("session-one", [{"role": "user", "content": "Use the safer procedure"}]):
        one = json.loads(skill_manage("edit", "example", content="recommendation"))
        two = json.loads(skill_manage("edit", "example", content="recommendation"))
    assert one["observed"] and one["observation_id"] == two["observation_id"]
    assert skill.read_text(encoding="utf-8") == "original"
    rows = list_observations(kind="skills")
    assert len(rows) == 1 and rows[0]["source"]["session_id"] == "session-one"
    assert rows[0]["source"]["snapshot_sha256"]
    assert dispose(one["observation_id"], "accepted", "Reviewed against source")
    assert not list_observations(kind="skills")
    assert list_observations(kind="skills", status="accepted")[0]["disposition_note"]
    assert (tmp_path / "state" / "review-observations.sqlite3").stat().st_mode & 0o077 == 0
