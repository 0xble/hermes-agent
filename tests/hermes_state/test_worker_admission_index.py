"""Worker-adoption candidate lookup stays bounded by non-terminal admissions."""

from hermes_state import SessionDB
import hermes_state_runtime as runtime
from tests.fakes.runtime_owner import begin_runtime_epoch


_INDEX_NAME = "idx_session_admissions_worker_adoption"


def _adoption_candidate_plan(db, *, generation, owner_epoch, session_id):
    """EXPLAIN the exact candidate SQL _linked_worker_admission executes, captured by tracing."""
    executed = []
    with db._read_ctx() as conn:
        conn.set_trace_callback(executed.append)
        try:
            runtime._linked_worker_admission(conn, session_id, generation, owner_epoch, ("started", "unknown"))
        finally:
            conn.set_trace_callback(None)
        candidate = next(sql for sql in executed if "FROM session_admissions" in sql)
        return [row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + candidate)]


def test_worker_adoption_uses_partial_index_and_ignores_terminal_history(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("target", source="test")
        epoch = begin_runtime_epoch(db, instance_id="index-test")
        generation = 7
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET runtime_generation=? WHERE id=?", (generation, "target")
            )
        )

        def seed(conn):
            rows = []
            for number in range(1_000):
                rows.append(
                    (
                        f"terminal-{number}", f"request-{number}", "history", "target", '["target"]',
                        "{}", f"digest-{number}", "queue", "terminal", "completed", epoch, generation,
                    )
                )
            rows.extend(
                [
                    (
                        "live-started", "live-request", "worker", "target", '["target"]', "{}", "live-digest",
                        "queue", "started", None, epoch, generation,
                    ),
                    (
                        "other-unknown", "other-request", "worker", "target", '["target"]', "{}", "other-digest",
                        "queue", "unknown", None, epoch, generation + 1,
                    ),
                    (
                        "queued", "queued-request", "worker", "target", '["target"]', "{}", "queued-digest",
                        "queue", "queued", None, None, None,
                    ),
                ]
            )
            conn.executemany(
                """INSERT INTO session_admissions(
                    admission_id, request_id, principal_id, target_session_id, lineage_json,
                    payload_json, payload_digest, intent, status, outcome, owner_epoch, generation
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )

        runtime.register_worker_execution(
            db,
            epoch=epoch,
            execution_id="worker",
            session_id="target",
            generation=generation,
            kind="compute",
            adoption_secret="secret",
        )
        db._execute_write(seed)

        plan = _adoption_candidate_plan(db, generation=generation, owner_epoch=epoch, session_id="target")
        assert any(_INDEX_NAME in detail for detail in plan), plan
        assert not any("SCAN session_admissions" in detail for detail in plan), plan

        adopted = runtime.adopt_worker_execution(
            db,
            epoch=epoch,
            execution_id="worker",
            session_id="target",
            generation=generation,
            adoption_secret="secret",
        )
        assert adopted["status"] == "running"
        live = runtime.get_session_admission(db, admission_id="live-started")
        other = runtime.get_session_admission(db, admission_id="other-unknown")
        queued = runtime.get_session_admission(db, admission_id="queued")
        assert live is not None and live["status"] == "started"
        assert other is not None and other["status"] == "unknown"
        assert queued is not None and queued["status"] == "queued"
        with db._read_ctx() as conn:
            terminal_count = conn.execute(
                "SELECT COUNT(*) FROM session_admissions WHERE status='terminal'"
            ).fetchone()[0]
        assert terminal_count == 1_000
    finally:
        db.close()
