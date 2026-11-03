"""Run the exact previous functions against the same isolated store as the fix.

Set CRON_ORIGINAL_JOBS_SOURCE to the parent commit's cron/jobs.py to exercise
expected failing reproductions; the ordinary suite exercises only the candidate.
"""
import ast
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cron import jobs

NOW = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)


@pytest.fixture(params=[False, True], ids=["candidate", "original-red"])
def isolated_store(request, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name, value in {"CRON_DIR": tmp_path / "cron", "JOBS_FILE": tmp_path / "cron/jobs.json",
                        "OUTPUT_DIR": tmp_path / "cron/output"}.items():
        monkeypatch.setattr(jobs, name, value)
    monkeypatch.setattr(jobs, "_hermes_now", lambda: NOW)
    if request.param:
        source = os.environ.get("CRON_ORIGINAL_JOBS_SOURCE")
        if source:
            source_text = Path(source).read_text()
        else:
            import subprocess
            # Immutable parent source: exercise the real old functions, not a
            # hand-written approximation. Source archives have no Git history.
            baseline = "c47c3de728"
            proc = subprocess.run(["git", "show", f"{baseline}:cron/jobs.py"],
                                  cwd=Path(__file__).resolve().parents[2],
                                  capture_output=True, text=True, check=False)
            if proc.returncode:
                pytest.skip("immutable baseline unavailable in a source archive")
            source, source_text = f"{baseline}:cron/jobs.py", proc.stdout
        tree = ast.parse(source_text)
        names = {"create_job", "_retire_expired_oneshot"}
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        assert len(nodes) == len(names)
        namespace = dict(jobs.__dict__)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), source, "exec"), namespace)
        for name in names:
            monkeypatch.setattr(jobs, name, namespace[name])
        request.node.add_marker(pytest.mark.xfail(strict=True, reason="verified pre-fix behavior"))


def test_never_started_one_shot_payload_survives_297_seconds_lateness(isolated_store):
    due = (NOW - timedelta(seconds=297)).isoformat()
    jobs.save_jobs([{"id": "reproduction", "prompt": "approved body", "enabled": True,
                     "schedule": {"kind": "once", "run_at": due}, "next_run_at": due,
                     "repeat": {"times": 1, "completed": 0}, "deliver": "local"}])
    assert jobs.get_due_jobs() == []
    kept = jobs.get_job("reproduction")
    assert kept is not None, "original code deleted the approved payload"
    assert kept["state"] == "missed"
    assert kept["prompt"] == "approved body"


def test_61_character_checksum_is_rejected_at_creation(isolated_store):
    bad_digest = "ab" * 30 + "c"
    with pytest.raises(ValueError, match="64 hex"):
        jobs.create_job(prompt=f"Expected SHA-256: {bad_digest}", schedule="in 10m")
