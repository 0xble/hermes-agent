"""Rebase a loaded jobs snapshot's changed fields onto the locked current store."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Collection, Optional

_MISSING = object()


def _jobs_file_stamp(jobs_file: Path) -> Optional[tuple[int, int, int]]:
    """Shrink-merge fast-path stamp ``(mtime_ns, size, ino)``; ``None`` if unstatable. ``st_ino`` is
    included because every writer uses mkstemp+rename, so a same-size write in one mtime quantum
    can't false-match."""
    try:
        st = jobs_file.stat()
        return (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return None


def _record_load_stamp(stamp: Optional[tuple[int, int, int]]) -> None:
    """Remember jobs.json's stamp for the enclosing _jobs_lock() section (no-op outside one) so the
    save path can skip the shrink-merge when disk provably hasn't changed. Capture it BEFORE
    reading: a mid-read sibling then mismatches (fail-safe); stamping after would certify an
    unseen write.

    Stamping after the read would let that sibling's write be certified as "seen" without being in the
    loaded payload, wrongly suppressing the recovery. See #80703.
    """
    from cron import jobs
    if getattr(jobs._jobs_lock_state, "depth", 0):
        jobs._jobs_lock_state.load_stamp = stamp


def _unmerged_disk_jobs(
    jobs: list[dict[str, Any]], removed_ids: Optional[Collection[str]]
) -> list[dict[str, Any]]:
    """On-disk jobs missing from *jobs* and not intentionally removed. Stamp match => nothing
    landed, return without parsing; unreadable store => raise RuntimeError (fail closed: writing
    over an unknown baseline would silently drop every job in it)."""
    from cron import jobs as store
    stamp = getattr(store._jobs_lock_state, "load_stamp", None)
    if stamp is not None and store._jobs_file_stamp(store._current_cron_store().jobs_file) == stamp:
        return []
    disk_jobs = store._peek_jobs_unlocked()
    if disk_jobs is None:
        raise RuntimeError(
            f"Cron database corrupted; refusing to overwrite {store._current_cron_store().jobs_file}")
    seen = {str(j["id"]) for j in jobs if isinstance(j, dict) and j.get("id")}
    seen |= {str(i) for i in (removed_ids or ()) if i}
    recovered: list[dict[str, Any]] = []
    for disk_job in disk_jobs:
        if not isinstance(disk_job, dict) or not disk_job.get("id"):
            continue
        disk_id = str(disk_job["id"])
        if disk_id not in seen:
            recovered.append(disk_job)
            seen.add(disk_id)
    return recovered


def _merge_unexpected_disk_jobs(
    jobs: list[dict[str, Any]], *, removed_ids: Optional[Collection[str]] = None,
) -> list[dict[str, Any]]:
    """*jobs* plus on-disk jobs absent from a stale writer's payload, preserving concurrent
    creates committed before this writer acquired the lock. Deletes pass ``removed_ids``; never
    mutates *jobs*."""
    from cron import jobs as store
    recovered = store._unmerged_disk_jobs(jobs, removed_ids)
    if not recovered:
        return jobs
    store.logger.warning(
        "Preserved %d cron job(s) present on disk but missing from the "
        "in-memory save payload (concurrent create since a stale writer read) (#80624): %s",
        len(recovered), [j.get("id") for j in recovered])
    return jobs + recovered


class JobsSnapshot(list):
    """Keep the caller's own baseline, not the baseline of a later nested reader."""

    def __init__(self, records: list[dict[str, Any]], stamp: Any) -> None:
        super().__init__(records)
        self.base = copy.deepcopy(records)
        self.stamp = stamp

    def checkpoint(self) -> None:
        # The baseline advances only after a committed save, and only to values the caller saw.
        # The merged store may include newer unseen fields, so never certify its stamp as ours.
        self.base = copy.deepcopy(list(self))
        self.stamp = None


def _conflict(job_id: str) -> RuntimeError:
    return RuntimeError(f"Cron job {job_id!r} changed since load; re-read before retrying the write")


def _merge_record(base: dict, desired: dict, current: dict) -> dict:
    merged = dict(current)
    for field in base.keys() | desired.keys():
        before, after = base.get(field, _MISSING), desired.get(field, _MISSING)
        if before == after:
            continue  # Scheduler bookkeeping does not own unchanged user configuration.
        latest = current.get(field, _MISSING)
        if latest != before and latest != after:
            raise _conflict(str(desired.get("id")))
        if after is _MISSING:
            merged.pop(field, None)
        else:
            merged[field] = after
    return merged


def merge_snapshot(records: list[dict[str, Any]], removed_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    """Called only under an owned jobs lock. Never overwrite an unseen field or resurrect a delete.

    Plain lists are used by explicit recovery/create paths that already read inside their lock.
    A load_jobs result retains its baseline even when saved in a later critical section.
    """
    from cron import jobs

    if not isinstance(records, JobsSnapshot):
        return records
    if records.stamp is not None and records.stamp == jobs._jobs_file_stamp(jobs._current_cron_store().jobs_file):
        return records
    disk = jobs._peek_jobs_unlocked()
    if disk is None:
        raise RuntimeError("Cron database corrupted; refusing to overwrite an unknown baseline")
    base = {str(j["id"]): j for j in records.base if j.get("id")}
    current = {str(j["id"]): j for j in disk if isinstance(j, dict) and j.get("id")}
    for job_id in removed_ids:
        if job_id in base and current.get(job_id, _MISSING) != base[job_id]:
            raise _conflict(job_id)
    merged = []
    for record in records:
        job_id = str(record.get("id", ""))
        if job_id not in base:
            if job_id in current and current[job_id] != record:
                raise _conflict(job_id)
            merged.append(record)
        elif job_id not in current:
            raise _conflict(job_id)
        else:
            merged.append(_merge_record(base[job_id], record, current[job_id]))
    return merged
