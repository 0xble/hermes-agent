"""Commit envelopes distinguish thread CPU and keep existing writer identities."""

import os
import threading

import pytest

import hermes_state
import hermes_state_runtime as rt
from hermes_state import SessionDB
from tests.fakes.runtime_owner import begin_runtime_epoch
from tests.hermes_state.test_transcript_write_diagnostics import _Clock, _Connection, _warnings


@pytest.mark.parametrize("worker", [False, True])
def test_slow_commit_reports_cpu_tid_operation_and_wal_size(tmp_path, monkeypatch, caplog, worker):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_STATE_WRITE_STALL_S", "0")
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("s", "test")
        if worker:
            epoch = begin_runtime_epoch(db, instance_id="boot")
            rt.admit_session_input(db, epoch=epoch, principal_id="human", session_id="s", request_id="input", payload={})
            claim = rt.claim_session_input(db, epoch=epoch, session_id="s")
            assignment = dict(execution_id="worker", session_id="s", generation=claim["generation"])
            rt.register_worker_execution(db, epoch=epoch, **assignment, kind="compute", adoption_secret="private")
        clock = _Clock()
        conn = _Connection(db._conn, clock, commit=12.0)
        original_commit = conn.commit

        def commit():
            result = original_commit()
            clock.cpu += 0.25
            return result

        conn.commit = commit
        monkeypatch.setattr(db, "_conn", conn)
        monkeypatch.setattr(hermes_state, "time", clock)
        original_warning = hermes_state.logger.warning

        def emit_warning(message, *args, **kwargs):
            if message.startswith("state.db write transaction"):
                assert not db._lock.locked()
            return original_warning(message, *args, **kwargs)

        monkeypatch.setattr(hermes_state.logger, "warning", emit_warning)
        with caplog.at_level("WARNING", logger="hermes_state"):
            if worker:
                result = rt.mutate_worker_execution(db, epoch=epoch, **assignment, sequence=1,
                    operation="transcript.append", payload={"messages": [{"role": "assistant", "content": "secret payload"}]})
                assert result
            else:
                db.append_messages_batch("s", [{"role": "assistant", "content": "secret payload"}])
        warning, = _warnings(caplog)
        operation = "transcript.append" if worker else "SessionMessagesMixin.append_messages_batch"
        for field in ("commit_s=12.000", "commit_cpu_s=0.250", f"native_tid={threading.get_native_id()}",
                      f"operation={operation}", "wal_bytes="):
            assert field in warning
        wal = tmp_path / "state.db-wal"
        assert f"wal_bytes={wal.stat().st_size if wal.exists() else 0}" in warning
        assert f"pid={os.getpid()}" in warning
        assert "secret payload" not in warning
        assert db.get_messages("s")[-1]["content"] == "secret payload"
