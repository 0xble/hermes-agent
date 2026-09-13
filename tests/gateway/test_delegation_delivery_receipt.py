"""The gateway can settle only a canonically persisted owned presentation."""
import json
from types import SimpleNamespace
from gateway.delegation_delivery_receipt import mark_persisted_delegation_presentations
from tools import async_delegation as ad


def test_restart_input_keeps_required_persistence_with_delegation_metadata():
    from gateway.delegation_delivery_receipt import delivery_metadata_for_event
    event = SimpleNamespace(internal=True, metadata={
        "delegation_parent_task_id": "parent", "delegation_thread_refs": ["A"]},
        _restart_inbox_claim={"input_owner": "restart-owner"})
    metadata = delivery_metadata_for_event(event, "restart-owner")
    assert metadata["gateway_input_owner"] == "restart-owner"
    assert metadata.get("gateway_input_required") is True
    assert metadata["delegation_results"][0]["thread_refs"] == ["A"]
    event._restart_inbox_claim = None
    assert "gateway_input_required" not in delivery_metadata_for_event(event, "ordinary-owner")


def test_gateway_helper_requires_persistence_and_exact_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    owner = {"session_id": "parent", "session_key": "route"}
    metadata = {"parent_task_id": "a" * 32, "owner": owner,
                "owner_json": json.dumps(owner, sort_keys=True, separators=(",", ":")),
                "threads": [{"thread_ref": "A", "task_index": 0}]}
    ad._persist_dispatch({"delegation_id": "deleg_fixture", "session_key": "route",
        "parent_session_id": "parent", "dispatched_at": 1, "delegation_metadata": metadata})
    ad._persist_completion({"delegation_id": "deleg_fixture", "status": "completed"}, {"summary": "result"})
    assert ad.claim_completion_delivery("deleg_fixture", "claim")
    assert ad.admit_completion_delivery("deleg_fixture", "claim")
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", source="cli")
    message = {"role": "user", "_row_id": 1, "display_metadata": {
        "delegation_deliveries": [{"delegation_id": "deleg_fixture", "owner": owner}]}}
    deliveries = message["display_metadata"]["delegation_deliveries"]
    assert not mark_persisted_delegation_presentations([message], deliveries)
    message["_row_id"] = db.append_message("parent", "user", "result", display_metadata=message["display_metadata"])
    db.close()
    assert not mark_persisted_delegation_presentations([message], [{"delegation_id": "deleg_fixture", "owner": {"session_id": "foreign"}}])
    assert mark_persisted_delegation_presentations([message], deliveries) == 1
    assert not mark_persisted_delegation_presentations([message], deliveries)
    assert ad.get_durable_delegation("deleg_fixture")["delivery_state"] == "delivered"
