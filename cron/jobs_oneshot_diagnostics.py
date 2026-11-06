"""Operator-visible traces left in a job's output dir when the due scan or the dispatch-limit
guard removes a one-shot that never completed (``cron/jobs.py`` late-imports these)."""
import logging
from typing import Any

logger = logging.getLogger("cron.jobs")


def _write(job: dict[str, Any], text: str, what: str) -> bool:
    """Durably queue the owner failure before retirement; save the local trace best-effort."""
    from cron.jobs import _hermes_now, save_job_output
    from cron.oneshot_retirement import queue_retirement_alert
    queue_retirement_alert(job, text, what, now=_hermes_now())
    try:
        save_job_output(job.get("id", ""), text)
        return True
    except Exception as e:
        logger.debug("Failed to write %s diagnostic for job %r: %s", what, job.get("id"), e)
        return False


def write_wedged(job: dict[str, Any]) -> None:
    """Trace for a wedged one-shot removal: dispatch was claimed but mark_job_run never ran
    (interrupted mid-run); removing it silently would leave no output, error, or record.

    A spent dispatch without a current-arm completion may have partial effects. Leave an alert
    and local trace when recovery retires it, rather than replaying it (#73973).
    """
    from cron.jobs import _hermes_now
    from cron.jobs_oneshot import occurrence_finished, occurrence_started

    if not occurrence_started(job) or occurrence_finished(job):
        return  # Only a current-arm interrupted dispatch needs a partial-effects warning.
    repeat = job.get("repeat") or {}
    claim = job.get("run_claim") or {}
    written = _write(
        job,
        "# Cron job removed without producing output\n\n"
        f"- job id: {job.get('id')}\n"
        f"- name: {job.get('name')}\n"
        f"- dispatch claimed: {repeat.get('completed', '?')}/{repeat.get('times', '?')}\n"
        f"- run claimed at: {claim.get('at', 'unknown')} by {claim.get('by', 'unknown')}\n"
        f"- removed at: {_hermes_now().isoformat()}\n\n"
        "This one-shot job's dispatch was claimed, but the run never "
        "completed for this occurrence (no current completion stamp) — the scheduler "
        "process was most likely killed or restarted mid-execution. The "
        "job has been removed to stop it re-firing; recreate it to run "
        "again.\n",
        "wedged-oneshot")
    if written:
        logger.warning(
            "Job '%s': removed without a completed run — diagnostic written to "
            "its output directory",
            job.get("name", job.get("id", "?")))


def write_retired(
    job: dict[str, Any], next_run: str, *, dispatch_limit: bool = False,
) -> None:
    """Route both retirement gates through the same interrupted-versus-unrun diagnostic."""
    from cron.jobs import ONESHOT_GRACE_SECONDS, _hermes_now
    from cron.jobs_oneshot import occurrence_finished, occurrence_started

    if occurrence_started(job) and not occurrence_finished(job):
        # A consumed, unfinished dispatch may already have side effects.
        write_wedged(job)
        return
    reason = "dispatch limit reached" if dispatch_limit else "run time outside grace window"
    _write(
        job,
        f"# Cron job removed before firing ({reason})\n\n"
        f"- job id: {job.get('id')}\n"
        f"- name: {job.get('name')}\n"
        f"- scheduled run time: {next_run}\n"
        f"- grace window: {ONESHOT_GRACE_SECONDS}s\n"
        f"- removed at: {_hermes_now().isoformat()}\n\n"
        f"This one-shot was removed without running: {reason}. "
        "A re-armed record may have consumed its dispatch budget on a prior run. "
        "Recreate it (or use the Run button) to schedule it again.\n",
        "missed-oneshot")


def write_missed(job: dict[str, Any], next_run: str) -> None:
    """Trace for a never-ran one-shot retired outside the grace window (else it would just vanish).
    """
    from cron.jobs import ONESHOT_GRACE_SECONDS, _hermes_now

    _write(
        job,
        "# Cron job removed before firing (run time outside grace window)\n\n"
        f"- job id: {job.get('id')}\n"
        f"- name: {job.get('name')}\n"
        f"- scheduled run time: {next_run}\n"
        f"- grace window: {ONESHOT_GRACE_SECONDS}s\n"
        f"- removed at: {_hermes_now().isoformat()}\n\n"
        "This one-shot's run time is more than the grace window in the "
        "past (scheduler down past the window, host asleep, or jobs.json "
        "edited), which is outside the 'will never fire' contract "
        "enforced at create/update/resume time. The job was removed "
        "without running; recreate it (or use the Run button) to "
        "schedule it again.\n",
        "missed-oneshot")
