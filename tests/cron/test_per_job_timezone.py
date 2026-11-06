"""Per-job IANA zones across lifecycle, dispatch, and control surfaces."""

from __future__ import annotations

import logging
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
        monkeypatch.setattr("tools.cronjob_tools._origin_from_env", lambda *_args: None, raising=False)
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


def test_cli_edit_timezone_reaches_real_update_and_can_clear(store):
    from types import SimpleNamespace
    from hermes_cli.cron import cron_edit

    job = jobs.create_job(name="timezone CLI regression", schedule="0 8 * * *", prompt="test", deliver="local")
    assert cron_edit(SimpleNamespace(job_id=job["id"], job_timezone="America/Los_Angeles")) == 0
    edited = jobs.get_job(job["id"])
    assert edited["timezone"] == "America/Los_Angeles"
    assert "job_timezone" not in edited
    assert datetime.fromisoformat(edited["next_run_at"]).astimezone(LOS_ANGELES).hour == 8
    assert cron_edit(SimpleNamespace(job_id=job["id"], job_timezone="")) == 0
    cleared = jobs.get_job(job["id"])
    assert cleared.get("timezone") is None
    assert datetime.fromisoformat(cleared["next_run_at"]).astimezone(NEW_YORK).hour == 8
    assert cleared["next_run_at"] != edited["next_run_at"]


class TestDueScanDispatchesInTheJobZone:
    """The due scan, not only next-run computation, must read a zoned job's wall clock in its own
    zone: its stored offset legitimately differs from the profile's, which is not a migration."""

    @pytest.mark.parametrize(
        ("zone", "stored_next", "scan_at"),
        [
            # Manila is ahead of New York: the stored 08:00+08 wall clock is "in the future" when
            # read against New York's 20:00, the shape the offset-repair path mistook for a move.
            ("Asia/Manila", "2026-06-11T08:00:00+08:00", "2026-06-10T20:00:01-04:00"),
            # Los Angeles is behind: normalized into New York the instant reads 11:00, off the
            # profile-zone lattice for ``0 8 * * *``.
            ("America/Los_Angeles", "2026-06-10T08:00:00-07:00", "2026-06-10T11:00:01-04:00"),
        ],
    )
    def test_a_due_occurrence_in_the_job_zone_fires_once_and_advances_one_day(
        self, store, monkeypatch, zone, stored_next, scan_at,
    ):
        job = jobs.create_job(prompt="zoned", schedule="0 8 * * *", job_timezone=zone)
        raw = jobs.load_jobs()
        for record in raw:
            if record["id"] == job["id"]:
                record["next_run_at"] = stored_next
        jobs.save_jobs(raw)
        monkeypatch.setattr(jobs, "_hermes_now", lambda: datetime.fromisoformat(scan_at))
        monkeypatch.setattr(jobs, "_timezone_migration_catchups", 0)
        monkeypatch.setattr(jobs, "_timezone_migration_catchups_recent", [])

        due = jobs.get_due_jobs()

        assert [row["id"] for row in due] == [job["id"]]
        assert jobs.get_job(job["id"])["next_run_at"] == stored_next
        # A job-zone offset is its native representation, not a profile-zone migration.
        assert jobs.get_timezone_migration_catchup_stats()["timezone_migration_catchups"] == 0
        nxt = datetime.fromisoformat(jobs.compute_next_run(
            jobs.get_job(job["id"])["schedule"], scan_at, zone))
        assert nxt.astimezone(ZoneInfo(zone)).strftime("%H:%M") == "08:00"
        assert (nxt - datetime.fromisoformat(stored_next)).total_seconds() == 86400

    def test_an_edited_expression_on_a_zoned_job_still_reanchors_without_firing(self, store, monkeypatch):
        job = jobs.create_job(prompt="edited", schedule="0 9 * * *", job_timezone="Asia/Manila")
        raw = jobs.load_jobs()
        for record in raw:
            if record["id"] == job["id"]:
                record["next_run_at"] = "2026-06-11T08:00:00+08:00"  # computed under the old 0 8
        jobs.save_jobs(raw)
        monkeypatch.setattr(jobs, "_hermes_now", lambda: datetime.fromisoformat("2026-06-10T20:00:01-04:00"))

        assert jobs.get_due_jobs() == []
        stored = datetime.fromisoformat(jobs.get_job(job["id"])["next_run_at"])
        assert stored.astimezone(ZoneInfo("Asia/Manila")).strftime("%H:%M") == "09:00"


def test_a_zone_edit_supersedes_an_unclaimed_occurrence_from_the_old_zone(store, monkeypatch):
    """An occurrence the dispatcher took off the schedule but never claimed belongs to the old
    wall clock. After a zone edit the due scan must not restore it over the recomputed run: that
    fires the job at the old zone's hour (08:00 New York = 20:00 Manila), not at 08:00 Manila."""
    from cron.occurrences import pending_slot_stamp

    now = datetime.fromisoformat("2026-06-10T08:00:30-04:00")
    monkeypatch.setattr(jobs, "_hermes_now", lambda: now)
    job = jobs.create_job(prompt="moving", schedule="0 8 * * *", deliver="local")
    raw = jobs.load_jobs()
    for record in raw:
        if record["id"] == job["id"]:
            record["next_run_at"] = "2026-06-11T08:00:00-04:00"  # advanced past the slot below
            record["pending_slot"] = pending_slot_stamp("2026-06-10T08:00:00-04:00", now)
    jobs.save_jobs(raw)

    edited = jobs.update_job(job["id"], {"timezone": "Asia/Manila"})
    assert edited["next_run_at"] == "2026-06-11T08:00:00+08:00"

    assert jobs.get_due_jobs() == []
    stored = jobs.get_job(job["id"])
    assert stored["next_run_at"] == "2026-06-11T08:00:00+08:00"
    assert "pending_slot" not in stored


LA = ZoneInfo("America/Los_Angeles")
NY = ZoneInfo("America/New_York")
HK = ZoneInfo("Asia/Hong_Kong")

WEEKDAY_9AM = "0 9 * * 1-5"
DAILY_9AM = "0 9 * * *"


@pytest.fixture
def la_profile(monkeypatch):
    """Configure the profile zone the way production does: ``HERMES_TIMEZONE`` + cache reset."""
    monkeypatch.setenv("HERMES_TIMEZONE", "America/Los_Angeles")
    hermes_time.reset_cache()
    yield LA
    hermes_time.reset_cache()


@pytest.fixture
def cron_store(tmp_path, monkeypatch):
    """Isolated store + a frozen clock the test moves explicitly."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    class Clock:
        instant = datetime(2026, 10, 22, 12, 0, tzinfo=NY)

        def set(self, value):
            self.instant = value

        def now(self):
            # Production shape: _hermes_now() is aware in the PROFILE zone.
            return self.instant.astimezone(hermes_time.get_timezone() or LA)

    clock = Clock()
    monkeypatch.setattr(jobs, "_hermes_now", clock.now)
    with jobs.use_cron_store(tmp_path / "cron"):
        yield clock


def _add_job(schedule, *, timezone=None, grace_minutes=None, next_run_at):
    """Create a real job through the public door, then park its instant where the test needs it."""
    job = jobs.create_job(
        prompt="probe", schedule=schedule, model="fixture", deliver="local",
        timezone=timezone, grace_minutes=grace_minutes)
    stored = jobs.load_jobs()
    row = next(r for r in stored if r["id"] == job["id"])
    row["next_run_at"] = next_run_at.isoformat()
    jobs.save_jobs(stored)
    return job["id"]


class TestPinnedTimezoneIsDue:
    """§0 of the design: the pinned job must be DUE, and the repair path must not touch it."""

    @pytest.mark.parametrize("job_zone", ["America/New_York", "Asia/Hong_Kong"])
    def test_job_east_of_the_profile_zone_fires_on_time(
        self, la_profile, cron_store, job_zone, caplog,
    ):
        """East of the profile zone is the direction that used to be dropped every time."""
        nine_am = datetime(2026, 10, 22, 9, 0, tzinfo=ZoneInfo(job_zone))
        cron_store.set(nine_am)
        job_id = _add_job(WEEKDAY_9AM, timezone=job_zone, next_run_at=nine_am)
        caplog.set_level(logging.INFO, logger="cron.scheduler")

        due = [j["id"] for j in jobs.get_due_jobs()]

        assert due == [job_id], "the pinned occurrence is due and must be returned"
        stored = next(r for r in jobs.load_jobs() if r["id"] == job_id)
        assert stored["next_run_at"] == nine_am.isoformat(), (
            "a pinned job's instant must never be rewritten into the profile zone")
        assert "Recomputing cron run" not in caplog.text, (
            "the offset repair only exists for jobs that FOLLOW the profile zone")

    def test_two_pinned_jobs_keep_their_own_nine_am(self, la_profile, cron_store):
        """One store, one profile zone, two jobs: each fires at ITS 09:00, not the other's."""
        ny_job = _add_job(
            WEEKDAY_9AM, timezone="America/New_York",
            next_run_at=datetime(2026, 10, 22, 9, 0, tzinfo=NY))
        hk_job = _add_job(
            WEEKDAY_9AM, timezone="Asia/Hong_Kong",
            next_run_at=datetime(2026, 10, 23, 9, 0, tzinfo=HK))

        # 09:00 New York == 21:00 Hong Kong == 06:00 Los Angeles: only the NY job is due.
        cron_store.set(datetime(2026, 10, 22, 9, 0, tzinfo=NY))
        assert [j["id"] for j in jobs.get_due_jobs()] == [ny_job]
        # The tick claims the fired job and advances it in ITS zone — the write site that would
        # otherwise jump a slot (and the claim is what retires the pending_slot stamp).
        assert jobs.claim_job_for_fire(ny_job) is True

        # A day later, 09:00 Hong Kong (== 18:00 the previous day in LA) is the HK job's turn.
        cron_store.set(datetime(2026, 10, 23, 9, 0, tzinfo=HK))
        assert [j["id"] for j in jobs.get_due_jobs()] == [hk_job]
        advanced = next(r for r in jobs.load_jobs() if r["id"] == ny_job)
        assert datetime.fromisoformat(advanced["next_run_at"]).astimezone(NY).hour == 9

    def test_unpinned_job_still_follows_the_profile_zone(self, la_profile, cron_store):
        """The default is unchanged: absent pin == profile wall clock (no migration needed)."""
        nine_la = datetime(2026, 10, 22, 9, 0, tzinfo=LA)
        cron_store.set(nine_la)
        job_id = _add_job(WEEKDAY_9AM, next_run_at=nine_la)

        assert [j["id"] for j in jobs.get_due_jobs()] == [job_id]


class TestComputeNextRunUsesTheJobZone:
    def test_next_run_lands_on_the_job_wall_clock(self, la_profile):
        schedule = jobs.parse_schedule(WEEKDAY_9AM)
        base = datetime(2026, 10, 22, 10, 0, tzinfo=NY).isoformat()

        pinned = datetime.fromisoformat(
            jobs.compute_next_run(schedule, base, timezone="America/New_York"))
        inherited = datetime.fromisoformat(jobs.compute_next_run(schedule, base))

        assert (pinned.hour, pinned.minute) == (9, 0)
        assert pinned.astimezone(NY).date() == datetime(2026, 10, 23).date()
        # Same instant computed in the profile zone would land at 09:00 LOS ANGELES.
        assert inherited.astimezone(LA).hour == 9
        assert pinned != inherited

    def test_dst_transitions_hold_the_job_wall_clock(self, la_profile):
        """Spring-forward (Mar 8 2026) and fall-back (Nov 1 2026) days stay at 09:00 job time."""
        schedule = jobs.parse_schedule(DAILY_9AM)
        for base in ("2026-03-07T09:00:00-05:00", "2026-10-31T09:00:00-04:00"):
            nxt = datetime.fromisoformat(
                jobs.compute_next_run(schedule, base, timezone="America/New_York"))
            assert (nxt.astimezone(NY).hour, nxt.astimezone(NY).minute) == (9, 0), base

    def test_fall_back_second_occurrence_is_strictly_later(self, la_profile):
        """qwen-code#11723 class: no immediate re-fire inside the repeated autumn hour."""
        base = datetime(2026, 11, 1, 1, 15, tzinfo=NY, fold=1)  # 01:15 EST (second 01:15)
        nxt = datetime.fromisoformat(
            jobs.compute_next_run({"kind": "cron", "expr": "30 1 * * *"},
                                  base.isoformat(), timezone="America/New_York"))
        assert nxt.timestamp() > base.timestamp()


class TestInheritanceAndDegradation:
    def test_null_and_absent_are_the_same_job(self, la_profile, cron_store):
        explicit = jobs.create_job(
            prompt="null", schedule=WEEKDAY_9AM, model="fixture", deliver="local",
            timezone=None)
        absent = jobs.create_job(
            prompt="absent", schedule=WEEKDAY_9AM, model="fixture", deliver="local")
        rows = {r["prompt"]: r for r in jobs.load_jobs()}
        assert "timezone" not in rows["null"] and "timezone" not in rows["absent"]
        assert explicit["next_run_at"] == absent["next_run_at"]

    def test_clearing_the_pin_through_update_drops_the_key(self, la_profile, cron_store):
        job = jobs.create_job(
            prompt="p", schedule=WEEKDAY_9AM, model="fixture", deliver="local",
            timezone="Asia/Hong_Kong")
        jobs.update_job(job["id"], {"timezone": ""})
        assert "timezone" not in jobs.load_jobs()[0]

    def test_changing_the_pin_recomputes_the_stored_instant(self, la_profile, cron_store):
        job = jobs.create_job(
            prompt="p", schedule=WEEKDAY_9AM, model="fixture", deliver="local",
            timezone="America/New_York")
        before = jobs.load_jobs()[0]["next_run_at"]

        jobs.update_job(job["id"], {"timezone": "Asia/Hong_Kong"})
        after = jobs.load_jobs()[0]["next_run_at"]

        assert after != before, "the instant belongs to the old zone; it must move"
        assert datetime.fromisoformat(after).astimezone(HK).hour == 9

    def test_a_corrupt_stored_zone_does_not_break_the_scan(self, la_profile, cron_store):
        nine_la = datetime(2026, 10, 22, 9, 0, tzinfo=LA)
        cron_store.set(nine_la)
        job_id = _add_job(WEEKDAY_9AM, next_run_at=nine_la)
        stored = jobs.load_jobs()
        stored[0]["timezone"] = "Mars/Olympus_Mons"  # hand-edited store
        jobs.save_jobs(stored)

        due = [j["id"] for j in jobs.get_due_jobs()]

        assert due == [job_id], "a bad zone degrades to the profile zone, it does not hide the job"


class TestGraceMinutesOverride:
    """``grace_minutes`` replaces the cadence-derived window for ONE job.

    Each case uses an hourly job 45 minutes late: the computed window for a 1h cadence is 30min
    (half the period, floored at 120s... capped at 2h), so the late run is a ``catch_up``;
    a 120-minute override puts the same run inside the window and it is merely ``late``. The
    dispatch stamp is what the dashboard and health check read, so it must follow the override.
    """

    def _stamp_for(self, la_profile, cron_store, *, grace_minutes):
        now = datetime(2026, 10, 22, 12, 45, tzinfo=LA)
        cron_store.set(now)
        job_id = _add_job(
            "0 * * * *", timezone="America/Los_Angeles", grace_minutes=grace_minutes,
            next_run_at=datetime(2026, 10, 22, 12, 0, tzinfo=LA))
        due = [j["id"] for j in jobs.get_due_jobs()]
        assert due == [job_id], "the missed occurrence still fires; only its classification moves"
        stored = next(r for r in jobs.load_jobs() if r["id"] == job_id)
        return stored["last_dispatch"]["kind"]

    def test_computed_window_classifies_a_45_min_late_hourly_run_as_catch_up(
        self, la_profile, cron_store,
    ):
        assert self._stamp_for(la_profile, cron_store, grace_minutes=None) == "catch_up"

    def test_override_widens_the_window_so_the_same_run_is_only_late(
        self, la_profile, cron_store,
    ):
        assert self._stamp_for(la_profile, cron_store, grace_minutes=120) == "late"

    def test_zero_grace_skips_outright_when_catch_up_is_disabled(
        self, la_profile, cron_store, tmp_path,
    ):
        """``0`` = never catch up. Hermes still runs a beyond-window miss once by default
        (``cron.catch_up_missed``); with catch-up disabled the zero window is what makes the
        skip, while a job with a wide override still fires."""
        (tmp_path / "config.yaml").write_text(
            "cron:\n  catch_up_missed: false\n", encoding="utf-8")
        now = datetime(2026, 10, 22, 12, 0, tzinfo=LA)
        cron_store.set(now)
        instant = datetime(2026, 10, 22, 11, 0, tzinfo=LA)  # one hour late, on the lattice
        never = _add_job("0 11 * * *", timezone="America/Los_Angeles", grace_minutes=0,
                         next_run_at=instant)
        tolerant = _add_job("0 11 * * *", timezone="America/Los_Angeles", grace_minutes=120,
                            next_run_at=instant)

        due = {j["id"] for j in jobs.get_due_jobs()}

        assert tolerant in due, "a 2h window covers a 1h-late run"
        assert never not in due, "grace 0 + catch-up disabled means the miss is skipped"

    def test_grace_is_refused_on_a_non_cron_schedule(self, la_profile, cron_store):
        with pytest.raises(ValueError, match="grace_minutes only applies to cron"):
            jobs.create_job(
                prompt="p", schedule="every 5m", model="fixture", deliver="local",
                grace_minutes=30)

    @pytest.mark.parametrize("minutes, expected", [(0, 120), (45, 45 * 60)])  # 0 floors at tick tolerance
    def test_stored_grace_round_trips(self, la_profile, cron_store, minutes, expected):
        job = jobs.create_job(
            prompt="p", schedule=WEEKDAY_9AM, model="fixture", deliver="local",
            grace_minutes=minutes)
        stored = next(r for r in jobs.load_jobs() if r["id"] == job["id"])
        assert stored["grace_minutes"] == minutes, "0 must not be dropped as falsy"
        assert jobs._compute_grace_seconds(stored["schedule"], stored["grace_minutes"]) == expected


class TestSlotMovesFollowThePin:
    def test_manual_run_settle_recomputes_in_the_job_zone(self, la_profile, cron_store):
        """A finished extra fire whose slot passed takes the next natural occurrence: in the
        JOB's zone, or the stale-lattice guard later re-anchors past it (one lost run)."""
        from cron import extra_fire

        cron_store.set(datetime(2026, 10, 22, 12, 0, tzinfo=NY))
        job = {"schedule": jobs.parse_schedule(DAILY_9AM), "timezone": "Asia/Hong_Kong",
               "next_run_at": datetime(2026, 10, 22, 9, 0, tzinfo=HK).isoformat()}
        extra_fire.settle(job, cron_store.now().isoformat())
        assert datetime.fromisoformat(job["next_run_at"]) == datetime(2026, 10, 23, 9, 0, tzinfo=HK)

    def test_distribution_zone_change_recomputes_the_instant(self, la_profile, cron_store):
        from cron.job_definition import merge_job_definition

        cron_store.set(datetime(2026, 10, 22, 12, 0, tzinfo=NY))
        local = jobs.create_job(prompt="p", schedule=DAILY_9AM, model="fixture",
                                deliver="local", timezone="Asia/Hong_Kong")
        authored = {"prompt": "p", "schedule": local["schedule"], "timezone": "America/New_York"}
        merged = merge_job_definition(dict(local), authored)
        assert datetime.fromisoformat(merged["next_run_at"]) == datetime(2026, 10, 23, 9, 0, tzinfo=NY)


class TestZeroGraceAtTickResolution:
    """The ticker fires seconds after the slot; ``0`` must mean "skip real misses", not "every
    run is a miss" (which, with catch-up disabled, silently stopped the job firing at all)."""

    @pytest.mark.parametrize("catch_up_disabled", [False, True])
    def test_on_time_tick_fires_as_scheduled(self, la_profile, cron_store, tmp_path, catch_up_disabled):
        if catch_up_disabled:
            (tmp_path / "config.yaml").write_text("cron:\n  catch_up_missed: false\n", encoding="utf-8")
        cron_store.set(datetime(2026, 10, 22, 9, 0, 20, tzinfo=NY))
        job_id = _add_job(DAILY_9AM, timezone="America/New_York", grace_minutes=0,
                          next_run_at=datetime(2026, 10, 22, 9, 0, tzinfo=NY))
        assert [j["id"] for j in jobs.get_due_jobs()] == [job_id]
        stored = next(r for r in jobs.load_jobs() if r["id"] == job_id)
        assert stored["last_dispatch"]["kind"] != "catch_up"

    def test_explicit_grace_skips_a_real_miss_even_with_default_catch_up(self, la_profile, cron_store):
        cron_store.set(datetime(2026, 10, 22, 10, 0, tzinfo=NY))
        job_id = _add_job(DAILY_9AM, timezone="America/New_York", grace_minutes=0,
                          next_run_at=datetime(2026, 10, 22, 9, 0, tzinfo=NY))
        assert jobs.get_due_jobs() == []
        stored = next(r for r in jobs.load_jobs() if r["id"] == job_id)
        assert datetime.fromisoformat(stored["next_run_at"]) == datetime(2026, 10, 23, 9, 0, tzinfo=NY)
