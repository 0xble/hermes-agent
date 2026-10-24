"""Private cron diagnostics, kept separate from delivery summaries."""

import contextvars
import logging
import time
from pathlib import Path
from traceback import format_exception

from agent.redact import redact_sensitive_text
from hermes_cli.immutable_releases import LOADED_RELEASE_ROOT
from hermes_constants import get_hermes_home, hermes_home_key

logger = logging.getLogger(__name__)
_dispatch_identity: contextvars.ContextVar[tuple[str, str | None]] = contextvars.ContextVar(
    "cron_dispatch_identity", default=("direct", None))


def dispatch_context(lane: str, code_sha: str | None):
    """Carry the actual admission lane and existing ledger identity across the thread hop."""
    ctx = contextvars.copy_context()
    ctx.run(_dispatch_identity.set, (lane, code_sha))
    return ctx


def note_dispatch_stage(job: dict, stage: str) -> None:
    """Keep stage timing local, metadata-only, and unable to fail a dispatch."""
    try:
        lane, code_sha = _dispatch_identity.get()
        fields = {
            "cron_dispatch_stage": stage,
            "cron_execution_id": job.get("execution_id"),
            "cron_job_id": job["id"],
            "cron_profile_home": hermes_home_key(get_hermes_home()),
            "cron_dispatch_lane": lane,
            "cron_code_sha": code_sha or (LOADED_RELEASE_ROOT.name if LOADED_RELEASE_ROOT else None),
            "cron_monotonic_ns": time.monotonic_ns(),
        }
        logger.info(
            "Cron dispatch execution=%s job=%s profile=%s lane=%s code=%s stage=%s monotonic_ns=%s",
            fields["cron_execution_id"], fields["cron_job_id"], fields["cron_profile_home"],
            fields["cron_dispatch_lane"], fields["cron_code_sha"], stage, fields["cron_monotonic_ns"],
            extra=fields,
        )
    # health: allow BLE001,S110 -- logging must not alter claims; events never carry exceptions
    except Exception:
        pass


def run_dispatched_job(process_job, job: dict):
    note_dispatch_stage(job, "executor_enter")
    return process_job(job)


# Enough for a Python traceback's last frames plus the exception line.
_WORKER_STDERR_TAIL_CHARS = 1500


def format_run_error(exc: BaseException) -> str:
    """Retain chained causes without capturing locals or exposing URL credentials."""
    traceback_text = redact_sensitive_text(
        "".join(format_exception(exc)), force=True, redact_url_credentials=True,
    )
    return f"## Error\n\n```\n{traceback_text}\n```\n"


def external_worker_stderr_tail(path: Path) -> str:
    """Redacted tail of a restart-safe cron worker's captured stderr, as a message suffix.

    The gateway used to spawn the worker with ``stderr=DEVNULL``, so a worker that died before
    publishing its acknowledgement (an import error, a missing module in the interpreter it was
    handed) only ever reported ``exit 1`` and the reporter had to guess the cause (#112729).
    Returns an empty string when nothing was captured.
    """
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    tail = text[-_WORKER_STDERR_TAIL_CHARS:]
    if len(text) > _WORKER_STDERR_TAIL_CHARS:
        tail = "…" + tail
    tail = redact_sensitive_text(tail, force=True, redact_url_credentials=True)
    return f"; worker stderr: {tail}"
