"""Focused tests for the cron jobs parse cache and no-op writer."""

import json
from unittest.mock import patch

from cron import jobs


def _document(job_id="job-1"):
    return {
        "jobs": [{"id": job_id, "prompt": "cache", "enabled": True}],
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


def test_load_jobs_reuses_mtime_cache_and_returns_safe_snapshots(tmp_path):
    with jobs.use_cron_store(tmp_path):
        jobs.save_jobs_document(_document())
        jobs._invalidate_jobs_cache()
        with patch.object(jobs, "_parse_jobs_file", wraps=jobs._parse_jobs_file) as parse:
            first = jobs.load_jobs()
            first[0]["prompt"] = "unsaved mutation"
            second = jobs.load_jobs()
        assert parse.call_count == 1
        assert second[0]["prompt"] == "cache"


def test_load_jobs_cache_hit_does_not_refresh_load_stamp(tmp_path):
    with jobs.use_cron_store(tmp_path):
        jobs.save_jobs_document(_document())
        jobs._invalidate_jobs_cache()
        with patch.object(jobs, "_stamp_is_racy", return_value=False):
            jobs.load_jobs()
            sentinel = (1, 2, 3, 4)
            with jobs._jobs_lock():
                jobs._jobs_lock_state.load_stamp = sentinel
                jobs.load_jobs()
                assert jobs._jobs_lock_state.load_stamp == sentinel


def test_load_jobs_cache_invalidates_on_external_rewrite(tmp_path):
    with jobs.use_cron_store(tmp_path):
        jobs.save_jobs_document(_document("before"))
        assert jobs.load_jobs()[0]["id"] == "before"
        path = tmp_path / "cron" / "jobs.json"
        path.write_text(json.dumps(_document("after"), indent=2), encoding="utf-8")
        assert jobs.load_jobs()[0]["id"] == "after"


def test_load_jobs_cache_isolated_between_cron_stores(tmp_path):
    with jobs.use_cron_store(tmp_path / "one"):
        jobs.save_jobs_document(_document("one"))
        assert jobs.load_jobs()[0]["id"] == "one"
    with jobs.use_cron_store(tmp_path / "two"):
        jobs.save_jobs_document(_document("two"))
        assert jobs.load_jobs()[0]["id"] == "two"


def test_save_jobs_document_skips_byte_identical_replace(tmp_path):
    with jobs.use_cron_store(tmp_path):
        document = _document()
        jobs.save_jobs_document(document)
        path = tmp_path / "cron" / "jobs.json"
        before = path.stat()
        with patch.object(jobs, "atomic_replace", wraps=jobs.atomic_replace) as replace:
            jobs.save_jobs_document(document)
        after = path.stat()
        assert replace.call_count == 0
        assert (after.st_ino, after.st_mtime_ns, after.st_size) == (
            before.st_ino, before.st_mtime_ns, before.st_size)


def test_changed_document_still_uses_atomic_replace(tmp_path):
    with jobs.use_cron_store(tmp_path):
        jobs.save_jobs_document(_document("before"))
        with patch.object(jobs, "atomic_replace", wraps=jobs.atomic_replace) as replace:
            jobs.save_jobs_document(_document("after"))
        assert replace.call_count == 1
        assert jobs.load_jobs()[0]["id"] == "after"
