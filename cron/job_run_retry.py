"""Rearm completed cron occurrences without consuming pre-start retry budgets."""

from datetime import datetime, timedelta


def _advance_after_run(job: dict, now: str, *, ladder_rung: bool = False) -> None:
    """Bump ``repeat.completed`` and recompute ``next_run_at``; retire the record as a terminal
    completion when the repeat limit is reached or a one-shot has no further run.

    ``ladder_rung``: this run re-ran an occurrence that already counted (an unreachable-model
    re-run, ``cron.unreachable_retry.is_retry_run``), so ``repeat.completed`` is left as is."""
    from cron.jobs import _complete_job_record, compute_next_run, logger

    # If no next run, decide whether this is terminal completion (one-shot) or a transient failure
    # (recurring schedule couldn't compute — e.g. 'croniter' missing from the runtime env). Recurring jobs
    # must NEVER be silently disabled: that turns a missing runtime dep into "job completed" and the user's
    # schedule quietly goes off. See issue #16265.
    kind = job.get("schedule", {}).get("kind")
    # One-shot dispatch-limit guard (issue #38758): a finite one-shot claimed via claim_dispatch() but whose
    # tick died before mark_job_run could remove it will have completed >= times while still looking due
    # (last_run_at was never written, so the recovery helper re-armed it). Remove it instead of re-firing.
    repeat = job.get("repeat")
    if repeat:
        times = repeat.get("times")
        finite = times is not None and times > 0
        completed = repeat.get("completed", 0)
        # Count each occurrence once. A ladder re-run's occurrence already counted, and finite
        # one-shots were pre-claimed by claim_dispatch() (completed already incremented); every
        # other run, recurring or direct, gets the increment.
        if not ladder_rung and not (kind == "once" and finite and completed > 0):
            completed += 1
            repeat["completed"] = completed
        if finite and completed >= times:
            # Limit reached: retain a terminal record instead of popping it, so the status just
            # written stays inspectable in `cronjob list`; the retention sweep prunes it later.
            _complete_job_record(job)
            return

    from cron.scheduler_admission import completion_next_run
    job["next_run_at"] = completion_next_run(
        job, compute_next_run(job["schedule"], now, job.get("timezone")))
    if job["next_run_at"] is not None:
        if job.get("state") != "paused":
            job["state"] = "scheduled"
    elif kind in {"cron", "interval"}:
        # Recurring: transient failure (e.g. croniter missing) — disabling it would turn a missing
        # dep into "job completed" and silently drop the schedule.
        job["state"] = "error"
        if not job.get("last_error"):
            job["last_error"] = (
                "Failed to compute next run for recurring schedule (is the 'croniter' package "
                "installed in the gateway's Python env?)")
        logger.error(
            "Job '%s' (%s) could not compute next_run_at; "
            "leaving enabled and marking state=error so the job is not silently disabled.",
            job.get("name", job.get("id", "?")), kind)
    else:
        _complete_job_record(job)  # one-shot: terminal completion


def preserve_unstarted_run(job: dict, now: str, *, ladder_rung: bool = False) -> None:
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
