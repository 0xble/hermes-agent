"""Keep a failed pre-start cron attempt from consuming its occurrence budget."""

from datetime import datetime, timedelta


def preserve_unstarted_run(job: dict, now: str, *, ladder_rung: bool = False) -> None:
    from cron.jobs import _advance_after_run

    if job.get("schedule", {}).get("kind") == "once":
        repeat = job.get("repeat") or {}
        if (
            repeat.get("times") is not None
            and repeat.get("times") > 0
            and not ladder_rung
        ):
            repeat["completed"] = max(0, repeat.get("completed", 0) - 1)
        # Refresh the retry instant so a store outage beyond the one-shot grace window
        # cannot turn this known unstarted attempt into a missed/retired occurrence.
        job["next_run_at"] = (
            datetime.fromisoformat(now) + timedelta(seconds=60)
        ).isoformat()
        if job.get("state") != "paused":
            job["state"] = "scheduled"
    else:
        _advance_after_run(job, now, ladder_rung=True)
