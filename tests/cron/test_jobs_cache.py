"""Focused tests for the cron jobs parse cache and no-op writer."""

import json
from pathlib import Path
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
        with patch.object(jobs, "_stamp_is_racy", return_value=False), \
                patch.object(jobs, "_parse_jobs_file", wraps=jobs._parse_jobs_file) as parse:
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
        with patch.object(jobs, "atomic_replace", wraps=jobs.atomic_replace) as replace, \
                patch.object(jobs, "_secure_file", wraps=jobs._secure_file) as secure, \
                patch.object(jobs, "_preserve_file_ownership", wraps=jobs._preserve_file_ownership) as preserve:
            jobs.save_jobs_document(document)
        after = path.stat()
        assert replace.call_count == 0
        assert secure.call_count == 1 and preserve.call_count == 1
        assert (after.st_ino, after.st_mtime_ns, after.st_size) == (
            before.st_ino, before.st_mtime_ns, before.st_size)


def test_save_jobs_document_skip_leaves_load_stamp_unrefreshed(tmp_path):
    with jobs.use_cron_store(tmp_path):
        document = _document()
        jobs.save_jobs_document(document)
        sentinel = (1, 2, 3, 4)
        with jobs._jobs_lock():
            jobs._jobs_lock_state.load_stamp = sentinel
            jobs._save_jobs_unlocked(document["jobs"], document=document)
            assert jobs._jobs_lock_state.load_stamp == sentinel


def test_skip_read_then_replace_does_not_serve_stale_cache(tmp_path):
    with jobs.use_cron_store(tmp_path):
        before = _document("before")
        after = _document("after")
        jobs.save_jobs_document(before)
        path = tmp_path / "cron" / "jobs.json"
        real_read_bytes = Path.read_bytes
        replaced = False

        def replace_after_read(file):
            nonlocal replaced
            payload = real_read_bytes(file)
            if not replaced:
                replaced = True
                path.write_bytes(json.dumps(after, indent=2).encode())
            return payload

        with patch.object(Path, "read_bytes", replace_after_read):
            with jobs._jobs_lock():
                jobs._save_jobs_unlocked(before["jobs"], document=before)
        assert jobs.load_jobs()[0]["id"] == "after"


def test_changed_document_still_uses_atomic_replace(tmp_path):
    with jobs.use_cron_store(tmp_path):
        jobs.save_jobs_document(_document("before"))
        with patch.object(jobs, "atomic_replace", wraps=jobs.atomic_replace) as replace:
            jobs.save_jobs_document(_document("after"))
        assert replace.call_count == 1
        assert jobs.load_jobs()[0]["id"] == "after"


def test_same_size_in_place_rewrite_in_one_second_is_seen(tmp_path):
    import os

    with jobs.use_cron_store(tmp_path):
        jobs.save_jobs_document(_document("before"))
        assert jobs.load_jobs()[0]["id"] == "before"
        path = tmp_path / "cron" / "jobs.json"
        before = path.stat()
        replacement = json.dumps(_document("change"), indent=2).encode()
        assert len(replacement) == path.stat().st_size
        path.write_bytes(replacement)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        assert after.st_size == before.st_size
        assert after.st_mtime_ns == before.st_mtime_ns
        assert jobs.load_jobs()[0]["id"] == "change"


def test_racy_parse_is_not_cached_under_its_post_read_stamp(tmp_path):
    with jobs.use_cron_store(tmp_path):
        jobs.save_jobs_document(_document("race"))
        path = tmp_path / "cron" / "jobs.json"
        jobs._invalidate_jobs_cache(path)
        stamp = (jobs.time.time_ns(), 1, 2, 3)
        with patch.object(jobs, "_jobs_file_stamp", side_effect=[stamp, stamp]), \
                patch.object(jobs, "_stamp_is_racy", return_value=True):
            parsed, _ = jobs._parse_jobs_file(path)
        assert parsed["jobs"][0]["id"] == "race"
        assert jobs._jobs_raw_parse_cache.get(jobs._jobs_cache_key(path)) is None


def test_stamp_change_between_read_and_stat_never_caches_under_later_stamp(tmp_path):
    with jobs.use_cron_store(tmp_path):
        jobs.save_jobs_document(_document("race"))
        path = tmp_path / "cron" / "jobs.json"
        jobs._invalidate_jobs_cache(path)
        before = (10, 1, 1, 1)
        during = (11, 1, 1, 1)
        # load_jobs sees the original stamp before and after the read, but _parse_jobs_file
        # observes a different stamp after reading. The payload is therefore not certified.
        with patch.object(jobs, "_jobs_file_stamp", side_effect=[before, before, during, before]):
            assert jobs.load_jobs()[0]["id"] == "race"
        assert jobs._jobs_parse_cache.get(jobs._jobs_cache_key(path)) is None
