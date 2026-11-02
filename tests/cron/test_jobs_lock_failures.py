"""Persistent lock failures must notify, while transient contention defers safely."""
import errno
import os
from datetime import timedelta
from unittest.mock import Mock

import pytest

from cron import jobs, store_health


@pytest.mark.platforms("posix")
@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0, reason="root bypasses lock-file permissions")
def test_unopenable_jobs_lock_degrades_store_and_notifies(tmp_path, monkeypatch):
    notices = Mock()
    monkeypatch.setattr(store_health, "_listener", notices)
    with jobs.use_cron_store(tmp_path / "home"):
        job = jobs.create_job(prompt="permission failure", schedule="every 1h")
        slot = (jobs._hermes_now() - timedelta(seconds=5)).isoformat()
        jobs.update_job(job["id"], {"next_run_at": slot})
        path = jobs._jobs_lock_file()
        path.chmod(0)
        try:
            jobs.get_due_jobs()
            record = store_health.degraded_record()
            assert record is not None
            assert "EACCES" in record.error
            notices.assert_called_once_with("unwritable", record)
            error = store_health.probe_store(path.parent)
            assert isinstance(error, OSError) and error.errno == errno.EACCES
            assert error.filename == str(path)
        finally:
            path.chmod(0o600)


@pytest.mark.platforms("posix")
def test_non_contention_flock_error_is_not_retried(monkeypatch):
    failure = OSError(errno.EIO, "lock backend I/O failure")
    flock = Mock(side_effect=failure)
    monkeypatch.setattr(jobs.fcntl, "flock", flock)
    with pytest.raises(OSError) as refused:
        jobs._acquire_flock(Mock(), 0)
    assert refused.value is failure
    flock.assert_called_once()


def test_idle_recovery_contention_warns_without_failure_and_retries(monkeypatch, caplog):
    jobs.save_jobs([])
    store_health.note_unwritable(OSError(errno.ENOSPC, "full"), "test", "scan")
    record = store_health.degraded_record()
    assert record is not None
    original_error = record.error
    monkeypatch.setattr(store_health, "probe_store", lambda _: None)
    acquire = Mock(return_value=False)
    monkeypatch.setattr(jobs, "_acquire_flock", acquire)
    store_health.recheck_idle()
    assert store_health.degraded_record() is record
    assert record.error == original_error
    assert "recovery deferred until the next tick" in caplog.text
    assert all(r.exc_info is None for r in caplog.records)
    # A contended recovery must not wait for the disk-failure probe throttle.
    acquire.return_value = True
    store_health.recheck_idle()
    assert store_health.degraded_record() is None
    assert acquire.call_count == 2
