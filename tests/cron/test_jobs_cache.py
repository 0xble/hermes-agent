"""Regression tests for the cron jobs-file parse and write caches."""

import json
import os

import pytest

from cron.jobs import load_jobs, save_jobs


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Redirect cron storage to a temp directory."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def test_load_jobs_reuses_parse_for_unchanged_stamp(tmp_cron_dir, monkeypatch):
    from cron import jobs

    jobs.ensure_dirs()
    jobs.JOBS_FILE.write_text(json.dumps({"jobs": [{"id": "a"}]}))
    calls = 0
    original = jobs._parse_jobs_file

    def counted(path):
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(jobs, "_parse_jobs_file", counted)
    assert load_jobs() == [{"id": "a"}]
    result = load_jobs()
    assert calls == 1
    result[0]["id"] = "changed"
    assert load_jobs()[0]["id"] == "a"


def test_load_jobs_reparses_after_atomic_replace(tmp_cron_dir, monkeypatch):
    from cron import jobs

    jobs.ensure_dirs()
    jobs.JOBS_FILE.write_text(json.dumps({"jobs": [{"id": "a"}]}))
    calls = 0
    original = jobs._parse_jobs_file

    def counted(path):
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(jobs, "_parse_jobs_file", counted)
    assert load_jobs()[0]["id"] == "a"
    replacement = jobs.JOBS_FILE.with_suffix(".tmp")
    replacement.write_text(json.dumps({"jobs": [{"id": "b"}]}))
    os.replace(replacement, jobs.JOBS_FILE)
    assert load_jobs()[0]["id"] == "b"
    assert calls == 2


def test_load_jobs_reparses_after_pinned_mtime_same_size_in_place_edit(tmp_cron_dir, monkeypatch):
    from cron import jobs

    jobs.ensure_dirs()
    jobs.JOBS_FILE.write_text(json.dumps({"jobs": [{"id": "a"}]}))
    assert load_jobs()[0]["id"] == "a"
    before = jobs.JOBS_FILE.stat()
    jobs.JOBS_FILE.write_text(json.dumps({"jobs": [{"id": "b"}]}))
    os.utime(jobs.JOBS_FILE, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = jobs.JOBS_FILE.stat()
    assert (after.st_size, after.st_mtime_ns, after.st_ino) == (
        before.st_size, before.st_mtime_ns, before.st_ino)
    assert load_jobs()[0]["id"] == "b"


def test_load_jobs_repairs_bare_list_to_canonical_document(tmp_cron_dir):
    from cron import jobs

    jobs.ensure_dirs()
    jobs.JOBS_FILE.write_text(json.dumps([{"id": "a"}]))
    assert load_jobs() == [{"id": "a"}]
    repaired = json.loads(jobs.JOBS_FILE.read_text())
    assert isinstance(repaired, dict)
    assert repaired["jobs"] == [{"id": "a"}]


def test_save_jobs_skips_semantic_noop_but_writes_change(tmp_cron_dir):
    from cron import jobs

    jobs.ensure_dirs()
    initial = [{"id": "a", "prompt": "one"}]
    save_jobs(initial)
    stamp = jobs.JOBS_FILE.stat()
    save_jobs(json.loads(json.dumps(initial)))
    assert (jobs.JOBS_FILE.stat().st_mtime_ns, jobs.JOBS_FILE.stat().st_ino) == (
        stamp.st_mtime_ns,
        stamp.st_ino,
    )
    changed = [{"id": "a", "prompt": "two"}]
    save_jobs(changed)
    after = jobs.JOBS_FILE.stat()
    assert (after.st_mtime_ns, after.st_ino) != (stamp.st_mtime_ns, stamp.st_ino)
