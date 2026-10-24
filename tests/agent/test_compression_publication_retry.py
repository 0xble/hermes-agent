"""Storage retries preserve the generated candidate and terminal fencing."""
import sqlite3
from unittest.mock import Mock, patch

import pytest

from agent.conversation_compression import CompressionCommitFence, _attempt_stall_fallback
from agent.conversation_compression_publication import retry_publication
from hermes_state import SessionDB
from hermes_state_errors import CompressionLeaseLostError
from hermes_state_publication import publication_metrics


@pytest.mark.parametrize("error", [
    sqlite3.DatabaseError("database disk image is malformed"),
    sqlite3.OperationalError("disk I/O error"),
    CompressionLeaseLostError("lost holder"),
    RuntimeError("closed parent"),
])
def test_terminal_publication_failure_is_not_replayed(error):
    publish = Mock(side_effect=error)
    with pytest.raises(type(error)):
        retry_publication(publish)
    publish.assert_called_once_with()


def test_busy_retry_rolls_back_each_failed_transaction_and_counts_attempted_rows(tmp_path):
    db = SessionDB(tmp_path / "rollback.db")
    calls = 0
    try:
        db.create_session("s", "test")
        def publish():
            nonlocal calls
            calls += 1
            def write(conn):
                assert conn.execute("SELECT title FROM sessions WHERE id='s'").fetchone()[0] is None
                conn.execute("UPDATE sessions SET title='published' WHERE id='s'")
                if calls < 3:
                    raise sqlite3.OperationalError("database is locked")
            return db._execute_write(write)
        with publication_metrics(0.1) as metrics, patch.object(db, "_sleep_before_write_retry", return_value=False):
            retry_publication(publish)
        assert calls == metrics["publish_attempts"] == 3
        assert metrics["publish_rows_written"] >= 3
        assert metrics["publish_wait_ms"] >= 0 and metrics["publish_hold_ms"] > 0
        assert db.get_session_title("s") == "published"
    finally:
        db.close()


@pytest.mark.parametrize("published", [False, True])
def test_model_fallback_is_only_allowed_before_publication_admission(published):
    fence = CompressionCommitFence()
    if published:
        assert fence.begin_commit()
        fence.finish_commit()
    with patch("agent.conversation_compression._retry_compression_on_fallback_chain", return_value="retry") as fallback:
        result = _attempt_stall_fallback(fence, worker=Mock())
    assert result == (None if published else "retry")
    assert fallback.call_count == (0 if published else 1)
