import pytest

from tools import async_delegation as ad


def test_thread_refs_are_stable_alphabetic_and_never_reused_for_exact_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    owner = {"profile": "p", "session_id": "s", "chat_id": "c", "topic_id": "t", "session_key": "k"}
    first = ad.reserve_delegation_metadata(parent_task_id=None, owner=owner, task_labels=["Fix warning", "Run task"])
    resumed = ad.reserve_delegation_metadata(parent_task_id=first["parent_task_id"], owner=owner, task_labels=["Resume work"])
    assert first["thread_refs"] == ["A", "B"]
    assert resumed["thread_refs"] == ["C"]
    assert first["task_labels"] == ["Fix warning", "Run task"]


def test_parent_identity_cannot_cross_immutable_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    owner = {"profile": "p", "session_id": "s", "chat_id": "c", "topic_id": "t", "session_key": "k"}
    parent = ad.reserve_delegation_metadata(parent_task_id=None, owner=owner, task_labels=[""])["parent_task_id"]
    other = {**owner, "topic_id": "other"}
    with pytest.raises(ValueError, match="immutable conversation owner"):
        ad.reserve_delegation_metadata(parent_task_id=parent, owner=other, task_labels=[""])


def test_empty_parent_identity_is_independent_and_uses_safe_label(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    owner = {"profile": "p", "session_id": "s", "chat_id": "c", "topic_id": "t", "session_key": "k"}
    metadata = ad.reserve_delegation_metadata(parent_task_id=None, owner=owner, task_labels=[""])
    assert isinstance(metadata["parent_task_id"], str) and metadata["parent_task_id"]
    assert metadata["task_labels"] == ["Run delegated task"]


def test_thread_refs_continue_past_z(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    owner = {"profile": "p", "session_id": "s", "chat_id": "c", "topic_id": "t", "session_key": "k"}
    metadata = ad.reserve_delegation_metadata(parent_task_id=None, owner=owner, task_labels=[""] * 28)
    assert metadata["thread_refs"][25:] == ["Z", "AA", "AB"]


def test_unknown_or_malformed_parent_reference_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    owner = {"profile": "p", "session_id": "s", "chat_id": "c", "topic_id": "t", "session_key": "k"}
    with pytest.raises(ValueError, match="32-character"):
        ad.reserve_delegation_metadata(parent_task_id="opaque-parent", owner=owner, task_labels=[""])
    with pytest.raises(ValueError, match="not a known"):
        ad.reserve_delegation_metadata(parent_task_id="a" * 32, owner=owner, task_labels=[""])


def test_durable_queries_require_exact_owner_and_preserve_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    owner = {"profile": "p", "session_id": "s", "chat_id": "c", "topic_id": "t", "session_key": "k"}
    metadata = ad.reserve_delegation_metadata(parent_task_id=None, owner=owner, task_labels=["Safe label"])
    metadata["threads"] = [{"thread_ref": "A", "task_label": "Safe label", "role": "worker"}]
    record = {"delegation_id": "metadata-durable", "goal": "secret raw goal", "context": None,
              "toolsets": None, "role": "worker", "model": None, "session_key": "k",
              "origin_ui_session_id": "", "origin_session_id": "", "parent_session_id": "s",
              "status": "running", "dispatched_at": 1.0, "delegation_metadata": metadata}
    ad._persist_dispatch(record)
    ad._persist_completion({"delegation_id": "metadata-durable", "status": "completed", "completed_at": 2.0}, {"summary": "done"})
    status = ad.get_delegation_status("metadata-durable", owner=owner)
    assert status and status["delegation_metadata"]["threads"][0]["thread_ref"] == "A"
    assert ad.get_delegation_result("metadata-durable", owner={**owner, "topic_id": "other"}) is None
    assert [x["delegation_id"] for x in ad.list_durable_delegations(owner=owner)] == ["metadata-durable"]


def test_completion_event_carries_safe_thread_metadata():
    metadata = {"parent_task_id": "opaque", "owner": {"profile": "p"}, "owner_json": "x",
                "threads": [{"thread_ref": "A", "task_label": "Safe label", "role": "worker"}]}
    fields = ad._completion_metadata_fields(metadata)
    assert fields["parent_task_id"] == "opaque"
    assert fields["thread_refs"] == ["A"]
    assert fields["task_labels"] == ["Safe label"]
