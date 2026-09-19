"""Per-job IANA timezone for cron expressions (Fork-Patch slice-12).

One profile schedules work for people in more than one region: the personal profile runs in
America/New_York while its LPG business-hours jobs are written for America/Los_Angeles. Upstream
reads every cron expression in the single profile zone, so ``0 8 * * *`` meant for Los Angeles
fired at 08:00 New York. A job may now carry its own zone; jobs without one are unchanged.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("croniter")

import hermes_time
from cron import jobs
from cron.jobs import compute_next_run, normalize_job_timezone

NEW_YORK = ZoneInfo("America/New_York")
LOS_ANGELES = ZoneInfo("America/Los_Angeles")
EIGHT_AM = {"kind": "cron", "expr": "0 8 * * *"}


@pytest.fixture
def new_york_profile(monkeypatch):
    monkeypatch.setenv("HERMES_TIMEZONE", "America/New_York")
    hermes_time.reset_cache()
    yield
    hermes_time.reset_cache()


@pytest.fixture
def store(tmp_path, monkeypatch, new_york_profile):
    home = tmp_path / ".hermes"
    cron_dir = home / "cron"
    cron_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(jobs, "HERMES_DIR", home)
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", cron_dir / "output")
    return cron_dir


def _next(last_run_at: str, timezone: str | None) -> datetime:
    return datetime.fromisoformat(compute_next_run(EIGHT_AM, last_run_at, timezone))


class TestComputeNextRunHonoursTheJobZone:
    def test_los_angeles_job_under_a_new_york_profile_fires_at_8am_los_angeles(self, new_york_profile):
        base = "2026-06-10T12:00:00-04:00"  # noon New York = 09:00 Los Angeles
        nxt = _next(base, "America/Los_Angeles")
        assert nxt.astimezone(LOS_ANGELES).strftime("%H:%M") == "08:00"
        assert nxt.astimezone(NEW_YORK).strftime("%H:%M") == "11:00"

    def test_a_job_without_a_zone_still_follows_the_profile(self, new_york_profile):
        base = "2026-06-10T12:00:00-04:00"
        nxt = _next(base, None)
        assert nxt.astimezone(NEW_YORK).strftime("%H:%M") == "08:00"

    @pytest.mark.parametrize("day", ["2026-03-08", "2026-11-01"])  # US spring-forward / fall-back
    def test_dst_transition_days_hold_the_job_zones_wall_clock(self, new_york_profile, day):
        base = f"{day}T00:30:00-05:00"
        nxt = _next(base, "America/Los_Angeles")
        assert nxt.astimezone(LOS_ANGELES).strftime("%H:%M") == "08:00"
        assert nxt.astimezone(LOS_ANGELES).date().isoformat() == day

    def test_a_zone_with_different_dst_rules_is_still_exact(self, new_york_profile):
        """Manila has no DST; the job must not drift with New York's transitions."""
        for base in ("2026-03-01T00:00:00-05:00", "2026-07-01T00:00:00-04:00"):
            nxt = _next(base, "Asia/Manila")
            assert nxt.astimezone(ZoneInfo("Asia/Manila")).strftime("%H:%M") == "08:00"


class TestValidation:
    def test_blank_means_follow_profile(self):
        assert normalize_job_timezone(None) is None
        assert normalize_job_timezone("  ") is None

    def test_invalid_zone_is_rejected_at_write_time(self):
        with pytest.raises(ValueError, match="Invalid IANA timezone"):
            normalize_job_timezone("America/Nowhere")
        with pytest.raises(ValueError):
            normalize_job_timezone(5)


class TestJobLifecycle:
    def test_create_stores_and_uses_the_zone(self, store):
        job = jobs.create_job(prompt="la job", schedule="0 8 * * *", job_timezone="America/Los_Angeles")
        assert job["timezone"] == "America/Los_Angeles"
        assert datetime.fromisoformat(job["next_run_at"]).astimezone(LOS_ANGELES).strftime("%H:%M") == "08:00"

    def test_create_rejects_a_bad_zone_before_storing(self, store):
        with pytest.raises(ValueError, match="Invalid IANA timezone"):
            jobs.create_job(prompt="bad", schedule="0 8 * * *", job_timezone="Not/AZone")
        assert jobs.load_jobs() == []

    def test_updating_only_the_zone_recomputes_next_run(self, store):
        job = jobs.create_job(prompt="move", schedule="0 8 * * *")
        before = datetime.fromisoformat(job["next_run_at"])
        assert before.astimezone(NEW_YORK).strftime("%H:%M") == "08:00"
        updated = jobs.update_job(job["id"], {"timezone": "America/Los_Angeles"})
        after = datetime.fromisoformat(updated["next_run_at"])
        assert after.astimezone(LOS_ANGELES).strftime("%H:%M") == "08:00"
        assert after != before

    def test_update_rejects_a_bad_zone(self, store):
        job = jobs.create_job(prompt="keep", schedule="0 8 * * *")
        with pytest.raises(ValueError, match="Invalid IANA timezone"):
            jobs.update_job(job["id"], {"timezone": "Mars/Olympus"})
        assert jobs.get_job(job["id"]).get("timezone") is None

    def test_legacy_record_layout_is_read_as_is(self, store):
        """The legacy fork stored ``timezone`` at job level; migrated files need no rewrite."""
        job = jobs.create_job(prompt="legacy", schedule="0 8 * * *")
        raw = jobs.load_jobs()
        for j in raw:
            if j["id"] == job["id"]:
                j["timezone"] = "America/Los_Angeles"
        jobs.save_jobs(raw)
        recomputed = jobs.compute_next_run(jobs.get_job(job["id"])["schedule"],
                                           None, jobs.get_job(job["id"]).get("timezone"))
        assert datetime.fromisoformat(recomputed).astimezone(LOS_ANGELES).strftime("%H:%M") == "08:00"


class TestSurfaces:
    def test_tool_create_and_update_carry_the_zone(self, store, monkeypatch):
        from tools.cronjob_tools import cronjob
        import json as _json
        monkeypatch.setattr("tools.cronjob_tools._origin_from_env", lambda: None, raising=False)
        out = _json.loads(cronjob(action="create", prompt="zoned", schedule="0 8 * * *", job_timezone="America/Los_Angeles"))
        assert out.get("success", True), out
        jid = out.get("job_id") or out.get("job", {}).get("id") or out.get("id")
        assert jobs.get_job(jid)["timezone"] == "America/Los_Angeles"
        out = _json.loads(cronjob(action="update", job_id=jid, job_timezone=""))
        assert jobs.get_job(jid).get("timezone") is None

    def test_cli_flags_are_forwarded(self):
        from hermes_cli.cron import _JOB_ARG_FIELDS, _job_api_kwargs
        from types import SimpleNamespace
        assert ("job_timezone", "job_timezone") in _JOB_ARG_FIELDS
        assert _job_api_kwargs(SimpleNamespace(job_timezone="Asia/Manila"))["job_timezone"] == "Asia/Manila"
