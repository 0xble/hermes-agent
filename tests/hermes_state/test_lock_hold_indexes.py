"""Plan and result contracts for the state.db write-lock hold indexes."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from gateway.session_ingress_media import _held_media_paths
from gateway.session_api_media import compact_settled_api_payloads
from hermes_state import SessionDB
from hermes_state_local import POLICY_PREFIX, moved_receipts
from persistence.sqlite.workers._context import worker_prompt
from persistence.sqlite.workers._lifecycle import insert_session_row_in_transaction


class _RecordingConnection:
    """Record SQL and bound parameters without expanding them like SQLite tracing does."""

    def __init__(self, conn, *, rewrite=None):
        self._conn = conn
        self.calls = []
        self._rewrite = rewrite

    def execute(self, sql, params=()):
        params = tuple(params)
        self.calls.append((sql, params))
        if self._rewrite:
            sql = self._rewrite(sql)
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _plan(conn, sql, params):
    return [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]


def _receipt(session_id, route, lineage=None):
    value = {"entry": {"session_id": session_id}, "route": route}
    if lineage is not None:
        value["lineage"] = lineage
    return json.dumps(value)


def test_moved_receipts_is_indexed_and_matches_not_indexed_for_all_target_modes(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        for sid, source in (("s1", "telegram"), ("s2", "slack"), ("s3", "cli")):
            db.create_session(sid, source, session_key=f"session:{sid}")
        db.create_session("s-null", "telegram")
        db._conn.executemany(
            "INSERT INTO state_meta(key, value) VALUES (?, ?)",
            [
                (POLICY_PREFIX + "owner-1", _receipt("s1", "other:1")),
                (POLICY_PREFIX + "owner-2", _receipt("s2", "other:2", ["s2", "s2-parent"])),
                (POLICY_PREFIX + "owner-3", _receipt("s3", "session:s3")),
                (POLICY_PREFIX + "owner-null-session", _receipt("s-null", "other:null")),
                (POLICY_PREFIX + "owner-null", _receipt("s1", None)),
                (POLICY_PREFIX + "malformed", "not json"),
                (POLICY_PREFIX + "missing", _receipt("missing", "other")),
                ("unrelated:key", _receipt("s1", "other")),
            ],
        )
        db._conn.commit()

        for target_ids in (None, {"s2"}):
            recorder = _RecordingConnection(db._conn)
            actual = moved_receipts(recorder, target_ids)
            sql, params = next(call for call in recorder.calls if "FROM state_meta" in call[0])
            assert "INDEXED BY idx_state_meta_local_policy_route" in sql
            assert "m.key GLOB ?" in sql
            assert POLICY_PREFIX + "*" in params
            assert ("json_each(?)" in sql) is (target_ids is not None)
            # SQLite cannot plan a forced partial index from a bound GLOB parameter. Keep the
            # production query parameterized, but use the equivalent literal only for plan proof.
            plan_sql = sql.replace("m.key GLOB ?", f"m.key GLOB '{POLICY_PREFIX}*'")
            plan_params = (params[0], *params[2:])
            assert "idx_state_meta_local_policy_route" in " ".join(_plan(db._conn, plan_sql, plan_params))

            reference_sql = plan_sql.replace(
                "state_meta m INDEXED BY idx_state_meta_local_policy_route", "state_meta m NOT INDEXED"
            )
            expected = {
                owner: (target, source)
                for owner, target, source in db._conn.execute(reference_sql, plan_params).fetchall()
            }
            assert actual == expected

        indexed = moved_receipts(db._conn)
        db._conn.execute("DROP INDEX idx_state_meta_local_policy_route")
        # Without the index the scan is slower but must still answer, never fail the boot heal or repoint.
        assert moved_receipts(db._conn) == indexed
        assert moved_receipts(db._conn, {"s2"}) == {k: v for k, v in indexed.items() if v[0] == "s2"}
    finally:
        db.close()


def _insert_admission(conn, seq_name, request_id, status, payload, principal_id="media-test"):
    conn.execute(
        """INSERT INTO session_admissions(
            admission_id, request_id, principal_id, target_session_id, lineage_json,
            payload_json, payload_digest, intent, status, owner_epoch, generation
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queue', ?, ?, ?)""",
        (seq_name, request_id, principal_id, "media-session", "[]", json.dumps(payload), seq_name, status,
         1 if status == "started" else None, 1 if status == "started" else None),
    )


def test_held_media_uses_partial_index_and_matches_not_indexed_after_mutations(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("media-session", "cli")
        media = lambda name: [{"path": f"/media/{name}", "sha256": "a" * 64}]
        _insert_admission(db._conn, "terminal-no-media", "r1", "terminal", {})
        _insert_admission(
            db._conn, "terminal-api", "r2", "terminal",
            {"text": "hello", "api_turn_v1": {"media": media("api"), "history": [{"role": "user"}]}},
            principal_id="api",
        )
        _insert_admission(db._conn, "queued", "r3", "queued", {"attachments_v1": {"media": media("queued")}})
        _insert_admission(db._conn, "started", "r4", "started", {"native_text_v1": {"media": media("started")}})
        _insert_admission(db._conn, "unknown", "r5", "unknown", {"text": "unknown"})
        _insert_admission(db._conn, "hosted-text", "hosted:6", "started", {"text": "hosted"})
        db._conn.commit()

        recorder = _RecordingConnection(db._conn)
        actual = _held_media_paths(recorder)
        sql, params = next(call for call in recorder.calls if "FROM session_admissions" in call[0])
        plan = _plan(db._conn, sql, params)
        assert "idx_session_admissions_held" in " ".join(plan), plan
        reference = _RecordingConnection(
            db._conn,
            rewrite=lambda query: query.replace("FROM session_admissions", "FROM session_admissions NOT INDEXED"),
        )
        assert actual == _held_media_paths(reference)

        # SQLite maintains the partial index as rows become terminal and as compact_settled_api_payloads
        # strips API media; both transitions must preserve the exact result set.
        db._conn.execute("UPDATE session_admissions SET status='terminal' WHERE admission_id='queued'")
        compact_settled_api_payloads(db, admission_id="terminal-api")
        db._conn.execute(
            "UPDATE session_admissions SET payload_json=json_remove(payload_json, '$.api_turn_v1.media') "
            "WHERE admission_id='terminal-api'"
        )
        db._conn.commit()
        assert _held_media_paths(_RecordingConnection(db._conn)) == _held_media_paths(reference)

        db._conn.execute("DROP INDEX idx_session_admissions_held")
        db._conn.commit()
        recorder = _RecordingConnection(db._conn)
        _held_media_paths(recorder)
        sql, params = next(call for call in recorder.calls if "FROM session_admissions" in call[0])
        with sqlite3.connect(db.db_path) as probe:
            assert "idx_session_admissions_held" not in " ".join(_plan(probe, sql + " /* dropped */", params))
    finally:
        db.close()


def test_targeted_prompt_delete_plan_and_reupsert_behavior(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        original = "original prompt"
        replacement = "replacement prompt"
        db.create_session("new-prompt-session", "cli", system_prompt="new prompt kept")
        assert db._conn.execute("SELECT COUNT(*) FROM system_prompts WHERE prompt='new prompt kept'").fetchone()[0] == 1
        db.create_session("prompt-session", "cli", system_prompt=original)
        original_hash = hashlib.sha256(original.encode()).hexdigest()
        replacement_hash = hashlib.sha256(replacement.encode()).hexdigest()
        assert db._conn.execute("SELECT 1 FROM system_prompts WHERE hash=?", (original_hash,)).fetchone()

        recorder = _RecordingConnection(db._conn)
        insert_session_row_in_transaction(
            db, recorder, "prompt-session", "cli", system_prompt=replacement
        )
        sql, params = next(call for call in recorder.calls if call[0].startswith("DELETE FROM system_prompts WHERE hash=?"))
        plan = _plan(db._conn, sql, params)
        assert any(detail.startswith("SEARCH system_prompts") for detail in plan), plan
        assert db._conn.execute("SELECT hash FROM system_prompts WHERE hash=?", (original_hash,)).fetchone()
        assert db._conn.execute("SELECT hash FROM system_prompts WHERE hash=?", (replacement_hash,)).fetchone() is None
    finally:
        db.close()


@pytest.mark.parametrize("worker", [False, True])
def test_prompt_replacement_collects_only_its_previous_reference(tmp_path, monkeypatch, worker):
    with SessionDB(db_path=tmp_path / "state.db") as db:
        pins = ["fixture-tool"]
        original = json.dumps(pins)
        db.create_session("owner", "cli", system_prompt=original)
        db.create_session("shared", "cli")
        db.update_session_tool_names("shared", pins)
        original_hash = hashlib.sha256(original.encode()).hexdigest()
        unrelated_hash = db._store_system_prompt(db._conn, "unrelated old orphan")
        recorder = _RecordingConnection(db._conn)

        if worker:
            worker_prompt(db, recorder, "owner", {"system_prompt": "updated"})
        else:
            monkeypatch.setattr(db, "_execute_write", lambda write, **_kwargs: write(recorder))
            db.update_system_prompt("owner", "updated")

        deletes = [(sql, params) for sql, params in recorder.calls if sql.startswith("DELETE FROM system_prompts")]
        assert len(deletes) == 1
        sql, params = deletes[0]
        assert params == (original_hash,)
        assert any(detail.startswith("SEARCH system_prompts") for detail in _plan(db._conn, sql, params))
        assert db.get_session("owner")["system_prompt"] == "updated"
        assert db._conn.execute("SELECT hash FROM system_prompts WHERE hash=?", (original_hash,)).fetchone()

        db.update_session_tool_names("shared", None)
        assert db._conn.execute("SELECT hash FROM system_prompts WHERE hash=?", (original_hash,)).fetchone() is None
        assert db._conn.execute("SELECT hash FROM system_prompts WHERE hash=?", (unrelated_hash,)).fetchone()
