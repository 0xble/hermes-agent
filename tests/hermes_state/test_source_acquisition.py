"""M0 fictional graph exclusion is enforced before SQLite prepares body reads."""

import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_state_evidence import EvidenceUnavailable, evidence_snapshot, inventory_rows


def test_control_closure_and_origin_preflight_precede_all_body_queries(tmp_path):
    from agent.source_origin import NativeOrigin
    from hermes_state_acquisition import AcquisitionPolicy, acquire_rows, graph_identity

    path = tmp_path / "fictional.db"
    db = SessionDB(path)
    graph = [("ancestor", None), ("control", "ancestor"), ("sibling", "ancestor"),
             ("foreign", "sibling"), ("hidden", "foreign"), ("archived", "hidden"),
             ("compacted", "archived"), ("rewound", "compacted"),
             ("dangling-control", "missing"), ("dangling-sibling", "missing"),
             ("cycle-a", "cycle-b"), ("cycle-b", "cycle-a"), ("allowed", None),
             ("marked-control", None), ("marked-child", "marked-control")]
    for sid, parent in graph:
        db.create_session(sid, source="oneshot" if sid == "control" else "telegram", parent_session_id=parent)
        db.append_message(sid, "user", "Goal contract: finish Lantern notice routing\nCompleted, permanently fixed.")
    directive = "Keep project progress notices off."
    for sid in ("allowed", "control"):
        proof = NativeOrigin.person(directive, platform="fictional", principal="person-1", event="event-positive")
        db.append_message(sid, "user", directive, platform_message_id="event-positive",
                          display_metadata={"_hermes_source_origin": proof})
    db.close()
    with sqlite3.connect(path) as writer:
        writer.execute("UPDATE sessions SET hidden=1, archived=1, profile_name='foreign' WHERE id='foreign'")
        writer.execute("UPDATE sessions SET created_source='audit_control' WHERE id='marked-control'")
        writer.execute("UPDATE messages SET active=0, compacted=1 WHERE session_id='compacted'")
        writer.execute("UPDATE messages SET active=0 WHERE session_id='rewound'")
        writer.commit()
    with evidence_snapshot(path) as conn:
        inventory = list(inventory_rows(conn))
        policy = AcquisitionPolicy(roots=("control", "dangling-control", "cycle-a"),
                                   inventory_sha256=graph_identity(inventory), coverage_complete=True)
        body_reads = []
        def guard(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and table == "messages" and column == "content":
                body_reads.append(column)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(guard)
        for sid, _ in graph:
            if sid == "allowed":
                continue
            with pytest.raises(EvidenceUnavailable):
                list(acquire_rows(conn, sid, policy))
        assert body_reads == []
        for invalid in (AcquisitionPolicy(roots=(), inventory_sha256=policy.inventory_sha256, coverage_complete=False),
                        AcquisitionPolicy(roots=("not-in-inventory",), inventory_sha256=policy.inventory_sha256,
                                          coverage_complete=True)):
            with pytest.raises(EvidenceUnavailable):
                list(acquire_rows(conn, "allowed", invalid))
        assert body_reads == []
        conn.set_authorizer(None)
        rows = list(acquire_rows(conn, "allowed", policy))
        assert [row.get("content") for row in rows if row["disposition"] == "eligible"] == [directive]
        assert rows[0]["disposition"] == "unresolved" and rows[0]["origin"] == "unknown"
    # An earlier successful inventory cannot authorize a later body snapshot after graph drift.
    with sqlite3.connect(path) as writer:
        writer.execute("UPDATE sessions SET parent_session_id='control' WHERE id='allowed'")
    with evidence_snapshot(path) as conn:
        conn.set_authorizer(guard)
        with pytest.raises(EvidenceUnavailable):
            list(acquire_rows(conn, "allowed", policy))
        assert body_reads == []
