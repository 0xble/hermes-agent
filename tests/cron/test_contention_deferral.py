"""Pre-agent contention must retain work, not manufacture a successful occurrence."""
import json
from datetime import datetime, timedelta

import pytest


@pytest.mark.parametrize("edit", [
    "same-dict", "same-string", "same-timezone", "schedule", "timezone",
    "next-run", "pause", "disable",
])
def test_deferred_occurrence_survives_only_non_schedule_edits(tmp_path, monkeypatch, edit):
    from cron import executions, jobs, scheduler
    from hermes_time import now

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "gate.py").write_text(
        'print(\'{"defer": {"reason": "writer busy", "retry_after_seconds": 30}}\')\n')
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda job: False)
    monkeypatch.setattr(scheduler, "_open_cron_session_db", lambda *a: pytest.fail("agent reached"))
    clock = now()
    monkeypatch.setattr(jobs, "_hermes_now", lambda: clock)
    job = jobs.create_job(prompt="Pending maintenance", schedule="0 21 * * *",
                          timezone="UTC", script="gate.py", deliver="local")
    jobs.update_job(job["id"], {"next_run_at": clock.isoformat()})
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(claimed, dict)
    assert scheduler.run_one_job(claimed)
    before = jobs.get_job(job["id"])
    original_execution = executions.get_execution(claimed["execution_id"])
    edits = {
        "same-dict": {"schedule": before["schedule"], "timezone": "UTC"},
        "same-string": {"schedule": "0 21 * * *", "timezone": " UTC "},
        "same-timezone": {"timezone": " UTC "},
        "schedule": {"schedule": "0 22 * * *"},
        "timezone": {"timezone": "America/New_York"},
        "next-run": {"next_run_at": (clock + timedelta(days=1)).isoformat()},
        "pause": {"state": "paused"},
        "disable": {"enabled": False},
    }
    changed = jobs.update_job(job["id"], {"name": "Renamed", **edits[edit]})
    assert changed["name"] == "Renamed"
    assert executions.get_execution(claimed["execution_id"]) == original_execution
    if not edit.startswith("same-"):
        assert not changed.get("deferred_run")
        assert not jobs.get_job(job["id"]).get("deferred_run")
        return
    assert changed["deferred_run"] == before["deferred_run"]
    assert changed["next_run_at"] == before["next_run_at"]
    assert jobs.get_job(job["id"])["deferred_run"] == before["deferred_run"]
    # Even after the catch-up grace window, the original occurrence remains due.
    clock += timedelta(days=2)
    assert [j["id"] for j in jobs.get_due_jobs()] == [job["id"]]
    retry = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(retry, dict)
    assert retry["_scheduled_instant"] == claimed["_scheduled_instant"]
    assert retry["fire_claim"]["run_id"] != claimed["fire_claim"]["run_id"]


@pytest.mark.parametrize("schedule", ["every 1h", "0 21 * * *"])
@pytest.mark.parametrize("null_schedule", [False, True])
@pytest.mark.parametrize("retry_mode", ["manual", "resume"])
def test_paused_manual_deferral_preserves_schedule(tmp_path, monkeypatch, schedule, null_schedule, retry_mode):
    from cron import executions, jobs, scheduler
    from hermes_time import now

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    gate = scripts / "gate.py"
    gate.write_text('print(\'{"defer": {"reason": "writer busy", "retry_after_seconds": 30}}\')\n')
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda job: False)
    monkeypatch.setattr(scheduler, "_open_cron_session_db", lambda *a: pytest.fail("agent reached"))
    clock = now()
    monkeypatch.setattr(jobs, "_hermes_now", lambda: clock)
    job = jobs.create_job(prompt="Paused maintenance", schedule=schedule, script="gate.py", deliver="local")
    jobs.pause_job(job["id"])
    snapshot = None if null_schedule else (clock + timedelta(hours=2)).isoformat()
    jobs.update_job(job["id"], {"next_run_at": snapshot})
    before = jobs.get_job(job["id"])
    claimed = jobs.claim_job_for_fire(job["id"], force=True, manual=True, preserve_paused=True, return_job=True)
    assert isinstance(claimed, dict)
    assert scheduler.run_one_job(claimed)
    after = jobs.get_job(job["id"])
    assert after["next_run_at"] == snapshot
    for key in ("enabled", "state", "paused_at", "paused_reason", "repeat", "last_status", "last_error", "last_run_at"):
        assert after.get(key) == before.get(key), key
    assert after["fire_claim"] is None
    assert not after.get("run_claim")
    row = executions.get_execution(claimed["execution_id"])
    assert row["status"] == "deferred"
    assert row["finished_at"]
    assert after["deferred_run"]["execution_id"] == row["id"]
    assert jobs.claim_job_for_fire(job["id"], force=True, manual=True, preserve_paused=True) is False
    clock += timedelta(seconds=31)
    assert jobs.get_due_jobs() == []
    assert jobs.claim_job_for_fire(job["id"]) is False
    if retry_mode == "resume":
        resumed = jobs.resume_job(job["id"])
        assert resumed["enabled"] is True
        # Explicit resume supersedes the old deferred occurrence with a fresh schedule.
        assert not resumed.get("deferred_run")
        assert jobs.get_due_jobs() == []
        clock = datetime.fromisoformat(resumed["next_run_at"]) + timedelta(seconds=1)
        assert [j["id"] for j in jobs.get_due_jobs()] == [job["id"]]
        retry = jobs.claim_job_for_fire(job["id"], return_job=True)
    else:
        retry = jobs.claim_job_for_fire(job["id"], force=True, manual=True, preserve_paused=True, return_job=True)
    assert isinstance(retry, dict)
    assert retry["fire_claim"]["by"] != claimed["fire_claim"]["by"]
    assert not retry.get("deferred_run")
    gate.write_text('print(\'{"wakeAgent": false}\')\n')
    assert scheduler.run_one_job(retry)
    final = jobs.get_job(job["id"])
    assert final["repeat"]["completed"] == before["repeat"]["completed"] + 1
    assert executions.get_execution(retry["execution_id"])["status"] == "completed"
    assert executions.get_execution(claimed["execution_id"]) == row
    if retry_mode == "manual":
        assert final["enabled"] is False
        assert final["state"] == "paused"
        assert final["next_run_at"] == snapshot
    else:
        assert final["enabled"] is True
        assert final["state"] == "scheduled"


@pytest.mark.parametrize("schedule,manual", [("every 1h", True), ("every 1h", False), ("0 21 * * *", False), ("1m", False)])
def test_deferred_attempt_retries_with_fresh_identity(tmp_path, monkeypatch, schedule, manual):
    from cron import executions, jobs, scheduler
    from cron.occurrences import completed_occurrence
    from hermes_time import now

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    gate = scripts / "gate.py"
    gate.write_text('print(\'{"defer": {"reason": "writer busy", "retry_after_seconds": 30}}\')\n')
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda job: False)
    monkeypatch.setattr(scheduler, "_open_cron_session_db", lambda *a: pytest.fail("agent reached"))
    monkeypatch.setattr(scheduler, "_capture_job_script_snapshot", lambda *a: pytest.fail("verifier reached"))
    clock = now()
    monkeypatch.setattr(jobs, "_hermes_now", lambda: clock)
    job = jobs.create_job(prompt="Pending maintenance", schedule=schedule, script="gate.py", deliver="local")
    jobs.update_job(job["id"], {"next_run_at": clock.isoformat(), "failure_streak": 21,
                              "last_status": "error", "last_error": "prior failure",
                              **({"manual_run_at": clock.isoformat(), "manual_run_prompt": "pending context"} if manual else {})})
    before = jobs.get_job(job["id"])
    assert before is not None
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(claimed, dict)
    assert scheduler.run_one_job(claimed)
    after = jobs.get_job(job["id"])
    assert after is not None
    assert not list((tmp_path / "cron" / "output").glob("**/*.md"))
    for key in ("manual_run_at", "manual_run_prompt", "failure_streak", "last_status", "last_error", "repeat", "enabled"):
        assert after.get(key) == before.get(key), key
    row = executions.get_execution(claimed["execution_id"])
    assert row is not None
    assert row["status"] == "deferred"
    assert row["error"] == "writer busy"
    assert row["finished_at"]
    assert not completed_occurrence(after, claimed.get("_scheduled_instant"))
    assert jobs.get_due_jobs() == []
    assert jobs.claim_job_for_fire(job["id"], return_job=True) is False
    clock += timedelta(days=2)  # pending work survives missed-slot and one-shot grace windows
    due = jobs.get_due_jobs()
    assert [j["id"] for j in due] == [job["id"]]
    jobs.advance_next_runs([job["id"]])  # ticker pre-dispatch advancement must not erase retry
    retry = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(retry, dict)
    assert retry.get("_scheduled_instant") == claimed.get("_scheduled_instant")
    assert retry["fire_claim"]["by"] != claimed["fire_claim"]["by"]
    assert retry["fire_claim"]["run_id"] != claimed["fire_claim"]["run_id"]
    from cron.deferral import DeferredRun, finish_deferred_run
    replacement = jobs.get_job(job["id"])
    assert not finish_deferred_run(claimed, DeferredRun("late result", 10),
                                   claimed["execution_id"], claimed["fire_claim"]["by"])
    assert jobs.get_job(job["id"]) == replacement
    assert executions.get_execution(claimed["execution_id"]) == row
    # Existing no-work gate still means a successful occurrence, not deferral.
    gate.write_text('print(\'{"wakeAgent": false}\')\n')
    assert scheduler.run_one_job(retry)
    final = jobs.get_job(job["id"])
    assert final is not None
    assert final.get("manual_run_prompt") is None
    assert final["repeat"]["completed"] == before["repeat"]["completed"] + 1
    assert final["last_status"] == "ok"
    assert executions.get_execution(retry["execution_id"])["status"] == "completed"
    assert executions.get_execution(claimed["execution_id"]) == row
    if not manual:
        assert completed_occurrence(final, claimed["_scheduled_instant"])


@pytest.mark.parametrize("case", ["stale", "recovery", "failed-script", "invalid", "invalid-zero", "invalid-large", "invalid-blank", "no-agent", "legacy-ledger"])
def test_deferral_fences_and_nondeferral_paths(tmp_path, monkeypatch, case):
    import sqlite3
    from cron import executions, jobs, scheduler
    from hermes_time import now

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    payload = {"defer": {"reason": "writer busy", "retry_after_seconds": 30}}
    if case.startswith("invalid"):
        payload["defer"]["retry_after_seconds"] = {"invalid": True, "invalid-zero": 0, "invalid-large": 3601, "invalid-blank": 30}[case]
        if case == "invalid-blank":
            payload["defer"]["reason"] = " "
    (scripts / "gate.py").write_text(
        "print(" + repr(json.dumps(payload)) + ")\n" + ("raise SystemExit(7)\n" if case == "failed-script" else ""))
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda job: False)
    monkeypatch.setattr(scheduler, "_open_cron_session_db", lambda *a: pytest.fail("agent reached"))
    if case == "legacy-ledger":
        # A real old database, with a terminal record and index to preserve during CHECK migration.
        path = tmp_path / "cron" / "executions.db"
        path.parent.mkdir(exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute("""CREATE TABLE executions (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
                process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started_at INTEGER,
                status TEXT NOT NULL CHECK(status IN ('claimed','running','completed','failed','unknown')),
                claimed_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT)""")
            conn.execute("INSERT INTO executions(id,job_id,source,process_id,pid,status,claimed_at) VALUES ('old','other','test','gone',1,'completed','2026-01-01')")
            conn.execute("CREATE INDEX old_job_index ON executions(job_id)")
    job = jobs.create_job(prompt="Pending maintenance", schedule="every 1h", script="gate.py",
                          deliver="local", no_agent=case == "no-agent")
    jobs.update_job(job["id"], {"next_run_at": now().isoformat(), "failure_streak": 21,
                              "last_status": "error", "last_error": "prior failure"})
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(claimed, dict)
    captured = {}
    if case == "stale":
        original = scheduler._run_job_script_with_claim_heartbeat
        def replace_owner(*args, **kwargs):
            result = original(*args, **kwargs)
            jobs.update_job(job["id"], {"fire_claim": {"by": "replacement", "at": now().isoformat(), "run_id": "new"}})
            captured["job"] = jobs.get_job(job["id"])
            captured["ledger"] = executions.get_execution(claimed["execution_id"])
            return result
        monkeypatch.setattr(scheduler, "_run_job_script_with_claim_heartbeat", replace_owner)
    original_finish = executions._finish_deferred_execution
    if case == "recovery":
        monkeypatch.setattr(executions, "_finish_deferred_execution", lambda *a: (_ for _ in ()).throw(OSError("injected ledger outage")))
    processed = scheduler.run_one_job(claimed)
    current = jobs.get_job(job["id"])
    row = executions.get_execution(claimed["execution_id"])
    assert row is not None and current is not None
    if case == "stale":
        assert current == captured["job"]
        assert row == captured["ledger"]
        return
    if case == "recovery":
        assert processed is False
        assert current["deferred_run"]["execution_id"] == row["id"]
        assert current["failure_streak"] == 21
        assert current["repeat"]["completed"] == 0
        monkeypatch.setattr(executions, "_finish_deferred_execution", original_finish)
        monkeypatch.setattr(executions, "_PROCESS_ID", "new-process")
        monkeypatch.setattr(executions, "_owner_is_live", lambda *a: False)
        assert executions.recover_interrupted_executions() == 0
        row = executions.get_execution(row["id"])
        assert row is not None
        assert row["status"] == "deferred"
        assert executions.finish_execution(row["id"], success=True) is None
    elif case == "failed-script" or case.startswith("invalid"):
        assert row["status"] == "failed"
        assert not current.get("deferred_run")
        assert current["failure_streak"] == 22
    elif case == "no-agent":
        assert row["status"] == "completed"
        assert not current.get("deferred_run")
    else:
        assert row["status"] == "deferred"
        assert executions.get_execution("old")["status"] == "completed"
        with executions._transaction() as conn:
            assert conn.execute("SELECT name FROM sqlite_master WHERE name='old_job_index'").fetchone()
        # Deferred attempts share bounded terminal retention, and recovery never rewrites them.
        monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 1)
        another = executions.create_execution("other", source="test")
        executions.finish_execution(another["id"], success=True)
        assert executions.get_execution(row["id"]) is None


@pytest.mark.parametrize("lost_ownership", ["fire", "ledger-owner", "ledger-missing"])
def test_scheduler_reports_refused_deferral_without_accounting(tmp_path, monkeypatch, lost_ownership):
    from cron import executions, jobs, scheduler

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "gate.py").write_text(
        'print(\'{"defer": {"reason": "writer busy", "retry_after_seconds": 30}}\')\n')
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda job: False)
    monkeypatch.setattr(scheduler, "_open_cron_session_db", lambda *a: pytest.fail("agent reached"))
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *a, **kw: pytest.fail("ordinary accounting reached"))
    job = jobs.create_job(prompt="Pending maintenance", schedule="1m", script="gate.py", deliver="local")
    claimed = jobs.claim_job_for_fire(job["id"], force=True, return_job=True)
    assert isinstance(claimed, dict)
    finish = scheduler.finish_deferred_run
    captured = {}

    def lose_ownership_then_finish(job, result, execution_id, owner):
        # Inject the race at the scheduler's finalizer boundary, after the real
        # pre-agent script has produced a DeferredRun and the ledger is running.
        assert executions.get_execution(execution_id)["status"] == "running"
        if lost_ownership == "fire":
            current = jobs.get_job(job["id"])
            jobs.update_job(job["id"], {"fire_claim": {**current["fire_claim"], "by": "replacement"}})
        else:
            with executions._transaction() as conn:
                if lost_ownership == "ledger-owner":
                    conn.execute("UPDATE executions SET process_id='replacement' WHERE id=?", (execution_id,))
                else:
                    conn.execute("DELETE FROM executions WHERE id=?", (execution_id,))
        captured["job"] = jobs.get_job(job["id"])
        captured["execution"] = executions.get_execution(execution_id)
        result = finish(job, result, execution_id, owner)
        assert result is False
        return result

    monkeypatch.setattr(scheduler, "finish_deferred_run", lose_ownership_then_finish)
    assert scheduler.run_one_job(claimed) is False
    assert jobs.get_job(job["id"]) == captured["job"]
    assert executions.get_execution(claimed["execution_id"]) == captured["execution"]
    assert not captured["job"].get("deferred_run")
    assert not list((tmp_path / "cron" / "output").glob("**/*.md"))
