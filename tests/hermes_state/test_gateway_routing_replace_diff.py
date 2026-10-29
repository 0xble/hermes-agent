"""A full routing-index save writes only what changed, with full-replace semantics.

Every session update full-saves the gateway routing index. Rewriting each of the scope's rows
(about 2,400 on the live store) per update multiplied WAL traffic under the shared writer lock.
"""

from __future__ import annotations

from hermes_state import SessionDB


def _rows(db, scope):
    with db._read_ctx() as conn:
        return {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT session_key, entry_json, updated_at FROM gateway_routing WHERE scope = ?", (scope,))}


def test_replace_writes_only_the_difference_and_keeps_replace_semantics(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.replace_gateway_routing_entries({"a": "1", "b": "2", "c": "3"}, scope="s")
        db.replace_gateway_routing_entries({"z": "other"}, scope="other-scope")
        before = _rows(db, "s")
        with db._lock:
            changes_before = db._conn.total_changes

        db.replace_gateway_routing_entries({"a": "1", "b": "2-new", "d": "4"}, scope="s")

        with db._lock:
            changed_rows = db._conn.total_changes - changes_before
        after = _rows(db, "s")
        assert {k: v[0] for k, v in after.items()} == {"a": "1", "b": "2-new", "d": "4"}
        assert after["a"] == before["a"], "an unchanged row must not be rewritten"
        assert changed_rows == 3  # delete c, update b, insert d
        assert _rows(db, "other-scope")["z"][0] == "other"

        db.replace_gateway_routing_entries({}, scope="s")
        assert _rows(db, "s") == {}
    finally:
        db.close()
