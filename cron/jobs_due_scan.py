"""Prefetch monotonic cron facts; preserve the exact locked lookup for due negatives."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class _Facts:
    completed: set = field(default_factory=set)
    dead: dict = field(default_factory=dict)


_facts: ContextVar[_Facts | None] = ContextVar("cron_prefetched_facts", default=None)


def _claim_key(claim):
    return str(claim.get("by") or ""), str(claim.get("at") or "")


def _instant(value):
    from cron.occurrences import scheduled_instant

    try:
        return scheduled_instant(value)
    except (OverflowError, TypeError):
        return None  # The locked base scan contains/repairs malformed timestamps.


@contextmanager
def prefetched_facts(records):
    """Read before the jobs flock; facts never outlive this call/profile/thread.

    Only positive completion is reusable. Negative preflight is not proof: a runner
    can publish completion before the jobs lock is acquired. Due negatives therefore
    retain base's exact locked lookup. Future slots need no ledger access at all.
    """
    from cron import jobs, occurrences
    from cron.constants import FIRE_CLAIM_SKEW_SECONDS

    facts = _Facts()
    latest = jobs._hermes_now() + timedelta(seconds=FIRE_CLAIM_SKEW_SECONDS)
    for job in records:
        if not isinstance(job, dict) or not job.get("id"):
            continue
        pending = job.get("pending_slot")
        slots = [job.get("next_run_at"), pending.get("scheduled_at") if isinstance(pending, dict) else None]
        for slot in slots:
            instant = _instant(slot)
            key = str(job["id"]), instant
            try:
                if (instant is not None and datetime.fromisoformat(instant) <= latest
                        and key not in facts.completed and occurrences.completed_occurrence(job, instant)):
                    facts.completed.add(key)
            except (OverflowError, TypeError):
                pass  # Leave malformed-record repair/containment to the locked base scan.
        for field_name in ("run_claim", "fire_claim", "pending_slot"):
            claim = job.get(field_name)
            if isinstance(claim, dict):
                facts.dead[_claim_key(claim)] = jobs._claim_owner_is_dead(claim)
    token = _facts.set(facts)
    try:
        yield
    finally:
        _facts.reset(token)


def completed_occurrence(job, instant, *, for_fire=False):
    from cron import jobs, occurrences

    facts = _facts.get()
    if facts is None:
        return occurrences.completed_occurrence(job, instant)
    instant = _instant(instant)
    if instant is None:
        return False
    if (str(job["id"]), instant) in facts.completed:
        return True
    if not for_fire and datetime.fromisoformat(instant) > jobs._hermes_now():
        return False
    # Never authorize a fire with a stale negative. This rare due-only read intentionally
    # remains locked, preserving rollback dedup against concurrent completion publishers.
    return occurrences.completed_occurrence(job, instant)


def claim_owner_is_dead(claim):
    from cron import jobs

    facts = _facts.get()
    if facts is None:
        return jobs._claim_owner_is_dead(claim)
    # Proven death can release a claim; live/unknown conservatively retains base's TTL.
    return facts.dead.get(_claim_key(claim), False)


def locked_scan():
    """Inner implementation of get_due_jobs(); must be called with _jobs_lock held."""
    from cron import jobs as store

    raw_jobs = store.load_jobs()
    scan = store._DueScan(raw_jobs, store._hermes_now())
    scan.needs_save = store._normalize_due_scan_records(raw_jobs)
    jobs = [store._apply_skill_fields(j) for j in copy.deepcopy(raw_jobs)]
    # One-shot run-claim TTL, resolved once per scan (see store._oneshot_run_claim_ttl_seconds).
    run_claim_ttl = store._oneshot_run_claim_ttl_seconds()

    # Retention sweep: completed one-shots are kept for inspection but must not accumulate forever.
    if store._sweep_completed_oneshots(raw_jobs, scan.now, removed_ids=scan.removed):
        scan.needs_save = True
        jobs = [j for j in jobs if scan.find(j.get("id")) is not None]

    due = []
    for job in jobs:
        # Per-job containment: one malformed record must never abort the whole scan. Normalization
        # above repairs known shapes; this catches FUTURE variants so healthy siblings still
        # run/persist.
        try:
            if store.is_terminal_job(job) and not store._is_recoverable_error_job(job):
                continue
            if not job.get("enabled", True):
                continue
            if store._has_pause_marker(job):
                store._self_disable_half_paused(job, scan)
                continue
            if store._evaluate_due_job(job, scan, run_claim_ttl):
                due.append(job)
        except Exception:
            store.logger.exception(
                "Skipping malformed cron job %r during due scan",
                job.get("name") or job.get("id") or "?")

    if scan.needs_save:
        try:
            store.save_jobs(raw_jobs, removed_ids=scan.removed or None)
        except OSError as exc:  # repairs live in memory; the next tick retries the persist
            store.store_health.note_unwritable(exc, "due-scan repairs not persisted", "scan")
            return due
    # Only now is a skipped one-shot claimed or durably due on disk: normal grace applies again.
    store.store_health.end_recovery_window(store._current_cron_store().cron_dir)
    for emit in scan.on_saved:
        emit()
    # Preserve the previous scan's evidence for every job in this scan. Capture
    # the scan instant, not tick/cycle completion (which another profile can stall).
    from cron.jobs_oneshot import record_due_scan
    record_due_scan(scan.now)
    return due



def with_prefetched_job(job_id, fn):
    from cron import jobs

    record = jobs.get_job(job_id)
    with prefetched_facts([record] if record else []):
        return fn()
