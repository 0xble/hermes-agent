"""Durable failure notices for one-shots retired without a completed run.

Jobs own retirement; the existing delivery outbox owns at-most-once sending. Commit
its stable receipt before deleting the job, so a crash between those commits retries
admission, never execution or an already attempted send.
"""
from __future__ import annotations

from datetime import UTC, datetime


def record_ticker_start() -> None:
    """Capture startup evidence before the first heartbeat overwrites the previous stamp."""
    from cron.jobs import _hermes_now, _read_marker_fields, _write_marker

    previous = _read_marker_fields("ticker_heartbeat")
    try:
        previous_at = datetime.fromtimestamp(float(previous[0]), UTC).isoformat()
    except (IndexError, ValueError, OverflowError, OSError):
        previous_at = ""
    _write_marker("ticker_start", f"{_hermes_now().isoformat()} {previous_at}", ".start_")


def queue_retirement_alert(job: dict, diagnostic: str, reason: str, *, now: datetime) -> None:
    from cron import delivery_queue
    from cron.jobs import _parse_aware, _read_marker_fields

    scheduled = (job.get("schedule") or {}).get("run_at") or job.get("next_run_at")
    scheduled_dt = _parse_aware(scheduled) if scheduled else None
    lateness = (f"{max(0, (now.astimezone(UTC) - scheduled_dt.astimezone(UTC)).total_seconds()):.0f}s"
                if scheduled_dt else "unknown")
    startup = _read_marker_fields("ticker_start")
    context = f"Scheduler started at {startup[0]}." if startup else "Scheduler start time unknown."
    if len(startup) > 1:
        context += f" Last heartbeat before startup: {startup[1]}; downtime onset is not known."
    interrupted = reason == "wedged-oneshot"
    outcome = ("Dispatch was interrupted without a completion record; partial effects may have "
               "occurred. Not rerun; inspect effects before you recreate to run."
               if interrupted else "Job not run; recreate to run.")
    content = (
        f"[CRON_FAILURE]\nOne-shot retired: {job.get('name') or job['id']}\n"
        f"Job id: {job['id']}\nScheduled time: {scheduled or 'unknown'}\n"
        f"Late by: {lateness} at {now.isoformat()}\n{outcome}\n{context}\n\n{diagnostic}"
    )
    # A retirement notice is not a successful report: never inherit review/local-only
    # output policy or a pinned success/failure lane that can hide it from its owner.
    notice = dict(job)
    for field in ("review", "output_local_only", "failure_deliver", "execution_id"):
        notice.pop(field, None)
    notice["deliver"] = "origin"
    # Retain the normal incident sink even on profiles with no configured chat.
    # Keep the signature stable when a retirement retries after a restart.
    from cron.scheduler import _upsert_incident_for_failure
    _, incident_id = _upsert_incident_for_failure(
        job, f"{reason}: one-shot {job['id']} scheduled at {scheduled}; {outcome}")
    notice["_failure_incident_id"] = incident_id
    # Keep the ordinary failure resolver's origin -> configured home fallback. No
    # inference run exists, so do not attach an execution terminality gate.
    receipt = f"oneshot-retired:{job['id']}"
    notice["execution_id"] = receipt
    delivery_queue.enqueue(receipt, notice, content, for_failure=True, terminal_gate=False)
