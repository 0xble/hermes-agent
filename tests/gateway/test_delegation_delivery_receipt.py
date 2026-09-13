"""The gateway can settle only a canonically persisted owned presentation."""
import json
from gateway.delegation_delivery_receipt import mark_persisted_delegation_presentations
from tools import async_delegation as ad


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
    with ad._transaction() as conn:
        conn.execute("CREATE TABLE messages(id INTEGER, session_id TEXT, role TEXT, display_metadata TEXT)")
    message = {"role": "user", "_row_id": 1, "display_metadata": {
        "delegation_deliveries": [{"delegation_id": "deleg_fixture", "owner": owner}]}}
    deliveries = message["display_metadata"]["delegation_deliveries"]
    assert not mark_persisted_delegation_presentations([message], deliveries)
    with ad._transaction() as conn:
        conn.execute("INSERT INTO messages VALUES (1,'parent','user',?)", (json.dumps(message["display_metadata"]),))
    assert not mark_persisted_delegation_presentations([message], [{"delegation_id": "deleg_fixture", "owner": {"session_id": "foreign"}}])
    assert mark_persisted_delegation_presentations([message], deliveries) == 1
    assert not mark_persisted_delegation_presentations([message], deliveries)
    assert ad.get_durable_delegation("deleg_fixture")["delivery_state"] == "delivered"
