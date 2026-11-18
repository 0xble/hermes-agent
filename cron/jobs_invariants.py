"""Execution-mode invariants every cron job definition must satisfy.

``create_job`` and ``update_job`` (``cron/jobs.py``) both run these, so an update cannot
bypass a rule the create door enforces.
"""
from typing import Optional

from hermes_constants import display_hermes_home

NO_AGENT_WITHOUT_SCRIPT_ERROR = (
    "no_agent=True requires a script — with no agent and no script "
    "there is nothing for the job to run."
)

SCRIPT_IS_COMMAND_LINE_ERROR = (
    "script must name a file under {scripts_dir} (e.g. 'watchdog.sh'), not a shell command line: "
    "{script!r} has no such file. Write the command into a script there and pass its filename."
)


def _validate_job_mode_invariants(
    monitor_script: Optional[str],
    monitor_url: Optional[str],
    no_agent: bool,
    script: Optional[str],
) -> None:
    """Execution-mode invariants shared by create_job and update_job (no bypass via the update
    door)."""
    if monitor_script and monitor_url:
        raise ValueError(
            "monitor_script and monitor_url are mutually exclusive — a job "
            "can only have one monitor source.")
    if (monitor_script or monitor_url) and no_agent:
        raise ValueError(
            "monitor_script/monitor_url cannot be combined with no_agent=True — "
            "the whole point of a monitor job is to suppress or wake the AGENT "
            "based on source changes. Use a plain no_agent script job instead.")
    if no_agent and not script:
        raise ValueError(NO_AGENT_WITHOUT_SCRIPT_ERROR)
    if script and _script_is_command_line(script):
        raise ValueError(SCRIPT_IS_COMMAND_LINE_ERROR.format(
            scripts_dir=display_hermes_home() + "/scripts/", script=script))


def validate_schedule_knobs(
    schedule: dict, timezone: Optional[str], grace_minutes: Optional[int],
) -> None:
    """Per-job schedule knobs (``timezone``/``grace_minutes``) are validated at BOTH doors —
    ``create_job`` and ``update_job`` — so an update cannot store a schedule knob a create
    would reject.

    Raises ValueError for an unknown IANA zone, and for ``grace_minutes`` on a schedule that has
    no wall clock to be late for (``once``/``interval``): the catch-up window only means
    something to a recurring cron cadence, so accepting it silently would suggest an effect it
    cannot have.
    """
    from cron.jobs_time import normalize_grace_minutes, normalize_timezone

    normalize_timezone(timezone)
    minutes = normalize_grace_minutes(grace_minutes)
    if minutes is None:
        return
    kind = (schedule or {}).get("kind")
    if kind != "cron":
        raise ValueError(
            f"grace_minutes only applies to cron schedules (a fixed wall clock that can be "
            f"missed); this job's schedule kind is {kind!r}.")


CATCH_UP_TICK_SECONDS = 60  # the ticker's default scan interval (cron/scheduler_provider.py)


def validate_catch_up_floor(schedule: dict, limit: Optional[float]) -> None:
    """Reject a catch-up deadline a protected interval (<= 2 min) cannot meet.

    These jobs start late by up to about one scheduler tick under ordinary load, because a busy
    tick dispatches after the deadline it scanned for and the fire claim then waits on the shared
    jobs lock. A strict deadline below one cadence plus one tick therefore skips routine
    occurrences and alerts the owner each time, while buying no freshness: the next occurrence is
    at most one cadence away. Longer intervals and cron schedules keep the documented strict
    semantics, including 0.
    """
    from cron.constants import PROTECTED_INTERVAL_MAX_SECONDS, is_protected_interval

    if limit is None or not is_protected_interval({"schedule": schedule}):
        return
    floor = float(schedule["minutes"]) * 60 + CATCH_UP_TICK_SECONDS
    if limit < floor:
        raise ValueError(
            f"max_catch_up_lateness_seconds={limit:g} is below {floor:g}s, the routine start "
            f"lateness of an interval of {PROTECTED_INTERVAL_MAX_SECONDS // 60} minutes or less "
            f"(one cadence plus one {CATCH_UP_TICK_SECONDS}s scheduler tick); occurrences would "
            f"be skipped routinely. Use at least {floor:g}, or omit it to never skip.")


def _script_is_command_line(script: str) -> bool:
    """A ``script`` with whitespace that resolves to no file is a command line (``echo hi``),
    not a path; run every tick it would deliver "Script not found" as the payload. A plain
    missing filename stays creatable (``hermes cron doctor`` reports it)."""
    if not any(ch.isspace() for ch in script.strip()):
        return False
    from cron.lifecycle_guard import _resolve_script_path
    path = _resolve_script_path(script)
    return path is None or not path.is_file()
