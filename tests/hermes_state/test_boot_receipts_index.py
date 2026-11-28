"""Boot's local-receipt scan reads the covering index, never every receipt's JSON.

The old query filtered with a bound ``LIKE``, which cannot imply the partial index's predicate, so
SQLite scanned all of state_meta and json-parsed every ``gateway.local_policy.v1`` receipt: 70k
receipts and 4.5 GB on the live store, about 8 minutes per call, 3 calls per boot.
"""

import json
from types import SimpleNamespace

from gateway.session_local_recovery import _BOOT_RECEIPTS_INDEX, _BOOT_RECEIPTS_SQL, local_receipt_ids
from gateway.session_viewer_close import CLOSE_PREFIX
from hermes_state import SessionDB
from hermes_state_local import POLICY_PREFIX

# The query this replaced, kept verbatim as the reference for set identity.
_OLD_SQL = (
    "SELECT m.key FROM state_meta m LEFT JOIN sessions s "
    "ON s.id = CASE WHEN json_valid(m.value) THEN json_extract(m.value, '$.entry.session_id') END "
    "WHERE m.key LIKE ? AND (s.ended_at IS NULL OR s.end_reason = 'compression' "
    "OR EXISTS (SELECT 1 FROM session_admissions a WHERE a.target_session_id = substr(m.key, ?) "
    "AND a.status IN ('queued','started','unknown')) "
    "OR EXISTS (SELECT 1 FROM state_meta c WHERE c.key = ? || substr(m.key, ?)))")


def _receipt(session_id):
    return json.dumps({"entry": {"session_id": session_id}, "route": None, "config_json": "x" * 512})


def _store(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    for sid in ("open", "ended", "compressed", "closed"):
        db.create_session(sid, "cli")
    db.end_session("ended", "agent_close")
    db.end_session("compressed", "compression")
    db.end_session("closed", "agent_close")
    db._conn.executemany("INSERT INTO state_meta(key, value) VALUES (?, ?)", [
        (POLICY_PREFIX + "owner-open", _receipt("open")),
        (POLICY_PREFIX + "owner-ended", _receipt("ended")),
        (POLICY_PREFIX + "owner-compressed", _receipt("compressed")),
        (POLICY_PREFIX + "owner-missing", _receipt("missing")),
        (POLICY_PREFIX + "owner-malformed", "not json"),
        (POLICY_PREFIX + "owner-closed", _receipt("closed")),
        (CLOSE_PREFIX + "owner-closed", "{}"),
        ("gateway.local_policyXv1:look-alike", _receipt("open")),
        ("unrelated:key", _receipt("open")),
    ])
    db._conn.commit()
    return db


def _old_ids(db):
    start = len(POLICY_PREFIX) + 1
    rows = db._conn.execute(_OLD_SQL, (POLICY_PREFIX + "%", start, CLOSE_PREFIX, start)).fetchall()
    return sorted(row[0][len(POLICY_PREFIX):] for row in rows)


def test_boot_receipts_read_the_covering_index_and_match_the_old_scan(tmp_path):
    db = _store(tmp_path)
    try:
        start = len(POLICY_PREFIX) + 1
        plan = " ".join(row[3] for row in db._conn.execute(
            "EXPLAIN QUERY PLAN " + _BOOT_RECEIPTS_SQL, (start, CLOSE_PREFIX, start)))
        assert f"COVERING INDEX {_BOOT_RECEIPTS_INDEX}" in plan
        actual = sorted(local_receipt_ids(SimpleNamespace(db=db)))
        assert actual == ["owner-closed", "owner-compressed", "owner-malformed", "owner-missing", "owner-open"]
        # LIKE treated '_' as a wildcard and ignored case; the literal GLOB only narrows that.
        assert actual == [sid for sid in _old_ids(db) if not sid.startswith("look")]
    finally:
        db.close()


def test_boot_receipts_still_answer_without_the_index(tmp_path):
    db = _store(tmp_path)
    try:
        indexed = sorted(local_receipt_ids(SimpleNamespace(db=db)))
        db._conn.execute(f"DROP INDEX {_BOOT_RECEIPTS_INDEX}")
        db._conn.commit()
        assert sorted(local_receipt_ids(SimpleNamespace(db=db))) == indexed
    finally:
        db.close()
