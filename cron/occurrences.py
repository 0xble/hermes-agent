"""Exact scheduled identities, independent of mutable jobs.json dispatch stamps, plus the
profile-local stale-schedule catch-up counter marker."""
from datetime import datetime, timedelta, timezone, UTC
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import time
import logging
import sqlite3

logger = logging.getLogger(__name__)


@dataclass
class _CompletionReadBudget:
    deadline: float
    busy: bool = False


_completion_read_budget: ContextVar[_CompletionReadBudget | None] = ContextVar("completion_read_budget", default=None)


@contextmanager
def completion_read_scan():
    """Share one brief SQLite wait across a scan, not one wait per stored job."""
    token = _completion_read_budget.set(_CompletionReadBudget(time.monotonic() + 0.05))
    try:
        yield
    finally:
        _completion_read_budget.reset(token)


def recover_unstarted_run_claim(job, scan):
    """A local dispatch that never entered the running set can retry on this scan."""
    from cron.jobs import _job_running_in_this_process, _machine_id

    claim = job.get("run_claim")
    if (job.get("schedule", {}).get("kind") == "once" and isinstance(claim, dict)
            and claim.get("by") == _machine_id() and not _job_running_in_this_process(job["id"])):
        # Preserve outage eligibility when dispatch was refused before taking its fire claim.
        job["run_claim"] = {"outage": True} if claim.get("outage") else None
        scan.persist(job["id"], run_claim=job["run_claim"])


def scheduled_instant(value):
    """Canonicalize aware instants; legacy/ambiguous values carry no exact identity."""
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None:
            return None
        return instant.astimezone(UTC).isoformat()
    except ValueError:
        return None


def _completed_rows(job_id, instant):
    """Read completion proof without the ledger's writer mutex or schema setup.

    The due scan holds jobs.json's cross-process lock. Taking the execution writer
    mutex here lets unrelated ledger work hold that global lock for an entire scan.
    WAL readers need neither that mutex nor a fresh schema/PRAGMA pass per job.
    A scan shares 50 ms for busy periods, then keeps the unknown-proof fail-open policy.
    """
    from cron.executions import _db_path, _transaction

    budget = _completion_read_budget.get()
    if budget is not None and budget.busy:
        return []
    timeout = 0.05 if budget is None else max(0.0, budget.deadline - time.monotonic())
    path = _db_path()
    if not path.exists():
        return []
    query = (
        "SELECT id, finished_at, claimed_at FROM executions "
        "WHERE job_id=? AND scheduled_instant=? AND status='completed'"
    )
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                     timeout=timeout)) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(query, (str(job_id), instant)).fetchall()
    except sqlite3.OperationalError as exc:
        if budget is not None and getattr(exc, "sqlite_errorcode", 0) & 0xff in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            budget.busy = True
        # Preserve the legacy reader's lazy migration contract. Established ledgers
        # never enter this writer path, and a missing/unreadable DB is not initialized.
        if "no such column: scheduled_instant" not in str(exc):
            raise
        with _transaction() as conn:
            return conn.execute(query, (str(job_id), instant)).fetchall()


def completed_occurrence(job, instant):
    """Unknown/failed/pruned attempts cannot prove completion: keep them eligible."""
    from cron.constants import FIRE_CLAIM_SKEW_SECONDS

    instant = scheduled_instant(instant)
    if instant is None:
        return False
    # A skewed early fire (see claim_job_for_fire) legitimately completes just before its slot.
    earliest_real = datetime.fromisoformat(instant) - timedelta(seconds=FIRE_CLAIM_SKEW_SECONDS)
    try:
        rows = _completed_rows(job['id'], instant)
        for row in rows:
            completed_at = scheduled_instant(row["finished_at"] or row["claimed_at"])
            # Legacy or malformed timestamps remain proof; only positively identified poison
            # rows — completions recorded before their claimed occurrence — are ignored.
            if completed_at is None or datetime.fromisoformat(completed_at) >= earliest_real:
                # Both dedup gates (due scan and fire claim) consume the slot on True without a
                # run or a ledger row, so this line is the only trace the skip leaves (#111414).
                logger.warning(
                    "Job '%s' (%s): scheduled occurrence %s was already completed by execution "
                    "%s (finished %s); skipping the due slot without a new run",
                    job.get("name", job.get("id")), job.get("id"), instant, row["id"],
                    row["finished_at"] or row["claimed_at"])
                return True
        return False
    except Exception:
        logger.warning("Cannot check completed occurrence for job %s", job['id'], exc_info=True)
        return False


# --- Pending slot: the occurrence a tick took off the schedule but has not yet claimed ---
#
# The tick advances a recurring job's ``next_run_at`` BEFORE dispatch (at-most-once: a crash
# mid-run must not re-fire on every restart). That leaves a window — advance done, fire claim
# not yet taken (interpreter finalizing, executor refusing work, process killed) — in which the
# process exiting loses the occurrence silently: the restarted scan sees a future ``next_run_at``
# and nothing ever ran (#107485). ``pending_slot`` is the durable record of that window: the due
# scan stamps it with the exact stored instant plus the stamping owner, ``claim_job_for_fire``
# (the point after which side effects may exist) clears it, and any explicit rewrite of the
# schedule drops it. A slot still pending once its owner is provably gone (or its lease has
# expired) was never claimed, so it is restored ONCE as the due instant and then flows through
# the ordinary late / fast-forward / ``cron.catch_up_missed`` policy — never N replays.

def pending_slot_stamp(next_run, now):
    """Store value for a recurring occurrence about to be handed to the dispatcher."""
    from cron.jobs import _machine_id

    return {"scheduled_at": next_run, "at": now.isoformat(), "by": _machine_id()}


def unclaimed_pending_slot(job, now):
    """Stored instant of a slot the dispatcher never claimed, or None.

    None for non-recurring jobs and malformed stamps (never a fire) and for a job still in this
    process's running set (its queued worker will claim and clear the slot itself). A stamp by
    THIS process on a job not running here is orphaned (dispatch refused). A stamp by another
    process is honoured while that owner may still be alive within the fire-claim lease — a
    second live gateway on the same store is mid-dispatch, not dead."""
    from cron.constants import FIRE_CLAIM_TTL_SECONDS
    from cron.jobs import _claim_is_live, _job_running_in_this_process, _machine_id

    pending = job.get("pending_slot")
    if not isinstance(pending, dict):
        return None
    slot = pending.get("scheduled_at")
    if job.get("schedule", {}).get("kind") not in {"cron", "interval"} or not isinstance(slot, str):
        return None
    try:
        datetime.fromisoformat(slot)
    except ValueError:
        return None
    if _job_running_in_this_process(str(job.get("id", ""))):
        return None
    if pending.get("by") != _machine_id() and _claim_is_live(pending, now, FIRE_CLAIM_TTL_SECONDS):
        return None
    return slot


def get_catch_up_occurrence_count() -> int:
    """Return the profile-local stale-schedule catch-up count."""
    from cron.jobs import _current_cron_store

    path = _current_cron_store().cron_dir / "catch_up_occurrences"
    try:
        return max(0, int(path.read_text(encoding="utf-8-sig").strip()))
    except (OSError, ValueError):
        return 0


def record_catch_up_occurrence() -> None:
    """Increment the profile-local stale-schedule catch-up counter, best effort."""
    from cron.jobs import _write_marker

    _write_marker("catch_up_occurrences", str(get_catch_up_occurrence_count() + 1), ".count_")
