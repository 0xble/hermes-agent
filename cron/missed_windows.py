"""Opt-in expiration policy for scheduled recurring cron occurrences."""

import math

FIELD = "max_catch_up_lateness_seconds"


def normalize_max_catch_up_lateness(value) -> float | None:
    """Unset is unlimited; zero is a strict deadline. Empty string clears an override."""
    if value is None or value == "":
        return None
    error = f"{FIELD} must be a finite, nonnegative number of seconds (or unset)."
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(error)
    try:
        limit = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(error) from exc
    if not math.isfinite(limit) or limit < 0:
        raise ValueError(error)
    return limit


def expired_occurrence(job: dict, instant, now, *, manual: bool = False) -> dict | None:
    """Describe a missed scheduled window without rewriting the original due slot."""
    from datetime import UTC, datetime
    from cron import jobs

    if manual or (job.get("schedule") or {}).get("kind") not in {"cron", "interval"}:
        return None
    limit = normalize_max_catch_up_lateness(job.get(FIELD))
    if limit is None or not instant:
        return None
    slot = jobs._ensure_aware(datetime.fromisoformat(instant)).astimezone(UTC)
    lateness = (now.astimezone(UTC) - slot).total_seconds()
    if lateness <= limit:
        return None
    return {"scheduled_at": slot.isoformat(), "skipped_at": now.isoformat(),
            "lateness_seconds": lateness, FIELD: limit}


def missed_window_message(job: dict, result: dict) -> str:
    return (
        f"skipped: missed window — {job.get('name') or job['id']} ({job['id']}); "
        f"scheduled {result['scheduled_at']}, {result['lateness_seconds']:g}s late "
        f"(maximum {result[FIELD]:g}s). No job code was run; "
        "the next scheduled occurrence remains eligible."
    )


def record_missed_window(job: dict, result: dict, now) -> str:
    """Mutate a locked record only after its owner receipt is durable; caller saves it."""
    from cron import jobs
    from cron.owner_alerts import queue_owner_failure

    next_run = jobs.compute_next_run(job["schedule"], now.isoformat(), job.get("timezone"))
    if not next_run:
        raise ValueError(f"Cannot advance missed window for cron job {job['id']}")
    message = missed_window_message(job, result)
    output = jobs.save_job_output(job["id"], message + "\n")
    queue_owner_failure(
        job, f"missed-window:{job['id']}:{result['scheduled_at']}", message,
        output_file=output)
    job.update(last_status="skipped", last_error=message, last_missed_window=result,
               next_run_at=next_run, state="scheduled")
    job.pop("pending_slot", None)
    job.pop("fire_claim", None)
    jobs.logger.warning("%s", message)
    return message


def skip_before_start(snapshot: dict) -> str | None:
    """Recheck a queued/detached occurrence under its claim fence using current job policy.

    A stale worker may decline its own execution but must never clear a newer
    owner's claim or advance a different occurrence. None means still eligible.
    """
    from cron import jobs
    from cron.occurrences import scheduled_instant

    instant = snapshot.get("_scheduled_instant")
    # Explicit None is an occurrence-free manual fire; don't infer a future slot.
    if not instant:
        return None
    expected = (snapshot.get("fire_claim") or {}).get("by")

    def apply(records, _i, job):
        if not job.get("enabled", True) or jobs._has_pause_marker(job) or job.get("state") == "completed":
            return None  # Existing cancellation/terminal controls own these transitions.
        now = jobs._hermes_now()
        result = expired_occurrence(job, instant, now)
        if result is None:
            return None
        message = missed_window_message(job, result)
        current = (job.get("fire_claim") or {}).get("by")
        if expected != current:
            return message  # No mutations/alerts for a stale claimant.
        previous = job.get("last_missed_window") or {}
        if scheduled_instant(previous.get("scheduled_at")) == result["scheduled_at"]:
            return message
        if expected is None:
            pending = (job.get("pending_slot") or {}).get("scheduled_at")
            candidate = pending or job.get("next_run_at")
            if scheduled_instant(candidate) != result["scheduled_at"]:
                return message
        record_missed_window(job, result, now)
        jobs.save_jobs(records)
        return message

    return jobs._under_fire_fence(
        snapshot["id"], lambda: jobs._with_job(snapshot["id"], apply, None))
