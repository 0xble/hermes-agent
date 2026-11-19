"""Retention makes physical progress without letting a stuck marker monopolize a pass."""
from contextlib import closing
import json
import time
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from hermes_state_deletion import stage_delete
from hermes_state_mutation_retirement import RETIRED_PREFIX, retire_prunable
from tests.hermes_state.test_prune_pass_bounds import _old_ended


@pytest.mark.parametrize("held_s", [None, 0.3])
def test_default_prune_finishes_an_indivisible_three_mb_message(tmp_path, monkeypatch, held_s):
    with closing(SessionDB(tmp_path / "state.db")) as db:
        db.create_session("oversized", source="cron")
        at = time.time() - 200 * 86400
        content = "".join(f"oversizedneedle {i:06x} 日本語の記録 " for i in range(70000))
        db.append_message("oversized", "user", content, timestamp=at)
        db.end_session("oversized", "done")
        db._execute_write(lambda conn: conn.execute(
            "UPDATE sessions SET started_at=?,ended_at=?,last_activity_at=? WHERE id='oversized'", (at, at, at)))
        if held_s is not None:
            import hermes_state_deletion as deletion
            import hermes_state_prune_budget as budgets
            delete_page, offset = deletion._delete_page, 0
            # Observe the same 300 ms hold on every host. Real FTS deletion stays
            # enabled; extra CPU work cannot reliably fit a bounded wall deadline.
            monkeypatch.setattr(budgets, "time", SimpleNamespace(
                monotonic=lambda: time.monotonic() + offset, sleep=time.sleep))
            def observed_page(conn, sid, selected, key, marker):
                nonlocal offset
                result = delete_page(conn, sid, selected, key, marker)
                if selected:
                    offset = held_s
                    # Drive the actual SQLite progress callback after the hold,
                    # then the same pre-commit guard as transaction settlement.
                    conn.execute("WITH RECURSIVE work(n) AS (SELECT 1 UNION ALL "
                                 "SELECT n+1 FROM work WHERE n < 5000) SELECT sum(n) FROM work").fetchone()
                    budgets.check_prune_transaction(held_s)
                return result
            monkeypatch.setattr(deletion, "_delete_page", observed_page)

        assert db.prune_sessions() == 1
        assert db._read_one("SELECT id FROM messages WHERE session_id='oversized'") is None
        assert db._read_one("SELECT id FROM sessions WHERE id='oversized'") is None
        assert "cleanup" not in json.loads(db.get_meta(RETIRED_PREFIX + "oversized"))
        assert db._read_one("PRAGMA integrity_check")[0] == "ok"


@pytest.mark.parametrize("oversized", [False, True])
def test_expiring_pending_marker_defers_once_per_pass_and_retries_first(tmp_path, monkeypatch, caplog, oversized):
    with closing(SessionDB(tmp_path / "state.db")) as db:
        _old_ended(db, "stuck")
        if oversized:
            db._execute_write(lambda conn: conn.execute(
                "UPDATE messages SET content=? WHERE session_id='stuck'", ("stuckneedle " * 262144,)))
        db.append_messages_batch("stuck", [{"role": "user", "content": "also pending", "timestamp": 1.0}] * 20)
        def stage(conn):
            retire_prunable(conn, ["stuck"], defer_messages=True)
            stage_delete(conn, ["stuck"])
            conn.execute("""CREATE TRIGGER stuck_delete BEFORE DELETE ON messages
                WHEN OLD.session_id='stuck' BEGIN
                  SELECT (WITH RECURSIVE work(n) AS
                    (SELECT 1 UNION ALL SELECT n+1 FROM work WHERE n < 50000000)
                    SELECT sum(n) FROM work);
                END""")
        db._execute_write(stage)
        for attempt in (1, 2):
            _old_ended(db, f"small-{attempt}")
            assert db.prune_sessions(max_seconds=60) == 1
            assert db._read_one("SELECT id FROM sessions WHERE id=?", (f"small-{attempt}",)) is None
            marker = json.loads(db.get_meta(RETIRED_PREFIX + "stuck"))["cleanup"]
            assert marker["attempts"] == attempt
            assert marker["last_expired_at"] > 0
        assert "stuck" in caplog.text
        assert "could not complete" in caplog.text and "writer ceiling" in caplog.text
        assert db.get_session("stuck") is None
        assert db._read_one("SELECT id FROM messages WHERE session_id='stuck'") is not None
        db._execute_write(lambda conn: conn.execute("DROP TRIGGER stuck_delete"))
        real_read = db._read_all
        def read(sql, params=()):
            if "SELECT s.id FROM sessions s WHERE" in sql:
                assert db._read_one("SELECT id FROM sessions WHERE id='stuck'") is None
            return real_read(sql, params)
        monkeypatch.setattr(db, "_read_all", read)
        assert db.prune_sessions(max_seconds=60) == 0
        assert "cleanup" not in json.loads(db.get_meta(RETIRED_PREFIX + "stuck"))


def test_finite_prune_collects_retired_spillover_even_without_new_victims(tmp_path, monkeypatch):
    from hermes_state_media import PREFIX, SPILLOVER_PREFIX
    import gateway.session_ingress_media as media
    root = (tmp_path / "media").resolve()
    monkeypatch.setattr(media, "_media_root", lambda: root)
    native = root / ("a" * 64) / "image.png"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"retired image")
    spill = tmp_path / "cache" / "spillover"
    spill.mkdir(parents=True)
    retired, shared = spill / "retired.txt", spill / "shared.txt"
    retired.write_text("retired output")
    shared.write_text("shared output")
    with closing(SessionDB(tmp_path / "state.db")) as db:
        db.set_meta(PREFIX + "native", json.dumps({"path": str(native), "sha256": "a" * 64, "size": 13}))
        _old_ended(db, "old")
        db._execute_write(lambda conn: conn.execute(
            "UPDATE messages SET content=? WHERE session_id='old'", (str(retired) + " " + str(shared),)))
        db.create_session("keep", source="cli")
        db.append_message("keep", "user", str(shared))
        assert db.prune_sessions(max_seconds=60) == 1
        assert not retired.exists()
        assert not native.exists()
        assert shared.exists()
        # An interrupted older pass can leave candidates with no new sessions to prune.
        retired.write_text("left from earlier pass")
        db.set_meta(SPILLOVER_PREFIX + retired.name, json.dumps(retired.name))
        assert db.prune_sessions(max_seconds=60) == 0
        assert not retired.exists()
        assert shared.exists()


def test_media_collection_expires_without_losing_candidates_or_holding_writer(tmp_path, monkeypatch):
    import gateway.session_ingress_media as media
    from hermes_state_media import PREFIX
    from hermes_state_prune_budget import current_budget
    root = (tmp_path / "media").resolve()
    native = root / ("b" * 64) / "image.png"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"retired")
    monkeypatch.setattr(media, "_media_root", lambda: root)
    scanned = []
    def expensive_history(conn, references, **kwargs):
        scanned.append(current_budget().deadline - time.monotonic())
        conn.execute("WITH RECURSIVE work(n) AS (SELECT 1 UNION ALL "
                     "SELECT n+1 FROM work WHERE n < 50000000) SELECT sum(n) FROM work").fetchone()
        return set()
    monkeypatch.setattr(media, "_history_mentions", expensive_history)
    with closing(SessionDB(tmp_path / "state.db")) as db:
        db.set_meta(PREFIX + "native", json.dumps({"path": str(native), "sha256": "b" * 64, "size": 7}))
        assert db.prune_sessions(max_seconds=60) == 0
        assert scanned and 0 < scanned[0] <= 2.0
        assert native.exists()
        assert db.get_meta(PREFIX + "native") is not None
        db.set_meta("foreground-after-media", "ok")
