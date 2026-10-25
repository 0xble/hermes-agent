"""Short-interval admission beside the unchanged FIFO general executor."""

import concurrent.futures
import logging
import threading
import time
from datetime import datetime

from cron.constants import is_protected_interval
from hermes_constants import hermes_home_key

logger = logging.getLogger("cron.scheduler")
_reserved_pools: dict[str, concurrent.futures.ThreadPoolExecutor] = {}
_reserved_claims: dict[str, tuple[str, object]] = {}
_loaded_protected: dict[str, tuple[str, ...]] = {}
_lock = threading.Lock()
_wake = threading.Event()
_tick_scans = threading.local()
_SLOT_WAKE_CHECK_SECONDS = 0.25


def reserved_job_id(jobs: list[dict], home) -> str | None:
    """Select in store order, not due order, so a later deadline cannot steal the reservation."""
    from cron.jobs import is_job_runnable, is_terminal_job

    eligible = tuple(
        str(job["id"]) for job in jobs
        if is_protected_interval(job) and is_job_runnable(job) and not is_terminal_job(job))
    key = hermes_home_key(home)
    with _lock:
        held = _reserved_claims.get(key)
        loaded = eligible
        if held is not None and held[0] not in eligible:
            loaded = (held[0], *eligible)
        if _loaded_protected.get(key) != loaded:
            _loaded_protected[key] = loaded
            if len(loaded) > 1:
                logger.error(
                    "Cron has %d protected interval jobs but only 1 reserved worker; "
                    "%s use the general pool best-effort (FIFO)", len(loaded), ", ".join(loaded[1:]))
    return eligible[0] if eligible else None


def submit_due_jobs(due_jobs: list[dict], pool, process_job) -> list:
    """Preserve general FIFO while sending only the selected protected job to its reserve."""
    from cron import jobs, scheduler

    home = scheduler._get_hermes_home()
    # The due subset omits future protected jobs, so reservation needs the full store order.
    reserved_id = reserved_job_id(jobs.load_jobs(), home)
    with _lock:
        held = _reserved_claims.get(hermes_home_key(home))
        if held is not None and held[0] != reserved_id:
            # Selection can change while the previous owner still holds its fenced slot.
            # Treat the replacement as overflow, keeping its place in the general FIFO.
            reserved_id = None
    futures = []
    # Start the reserved lane before general submission bookkeeping, without changing
    # the relative admission order of any general-pool jobs (including protected extras).
    for job in due_jobs:
        if job["id"] == reserved_id:
            future = submit_reserved(job, process_job)
            if future is not None:
                futures.append(future)
            break
    for job in due_jobs:
        if job["id"] == reserved_id:
            continue
        future = scheduler._submit_with_guard(job, pool, process_job)
        if future is not None:
            futures.append(future)
    return futures


def submit_reserved(job: dict, process_job):
    """Keep the reservation until the existing running-job release passes its process fence."""
    from cron import scheduler

    home = scheduler._get_hermes_home()
    key = hermes_home_key(home)
    token = object()
    with _lock:
        if key in _reserved_claims:
            return None
        pool = _reserved_pools.get(key)
        if pool is None:
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="cron-reserved")
            _reserved_pools[key] = pool
        _reserved_claims[key] = (job["id"], token)
    try:
        future = scheduler._submit_with_guard(job, pool, process_job, registration_owner=token)
    except BaseException:
        _release_unsubmitted(key, token)
        raise
    if future is None:
        _release_unsubmitted(key, token)
    return future


def _release_unsubmitted(key, token) -> None:
    with _lock:
        claim = _reserved_claims.get(key)
        if claim is not None and claim[1] is token:
            _reserved_claims.pop(key)


def worker_released(inflight_key: tuple, owner, *, wake: bool = True) -> None:
    """Called only AFTER the running claim passes the detached-process exit fence."""
    with _lock:
        claim = _reserved_claims.get(inflight_key[0])
        if claim is not None and claim[0] == inflight_key[1] and claim[1] is owner:
            _reserved_claims.pop(inflight_key[0])
    # Failed dispatches return their claim too, but have freed no worker capacity.
    if wake:
        _wake.set()


def discard_reserved_pools(home_keys) -> None:
    for key in home_keys:
        with _lock:
            pool = _reserved_pools.pop(key, None)
            _loaded_protected.pop(key, None)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=False)


def shutdown_reserved_pools() -> None:
    for pool in list(_reserved_pools.values()):
        pool.shutdown(wait=True, cancel_futures=False)
    _reserved_pools.clear()


def note_due_scan(started_at: datetime) -> None:
    """Only an ungated scan can leave a newly crossed deadline needing a prompt rescan."""
    from cron import scheduler

    if not hasattr(_tick_scans, "started"):
        _tick_scans.started = {}
    _tick_scans.started[hermes_home_key(scheduler._get_hermes_home())] = started_at


def next_due_wait(max_wait: float) -> float:
    """Read, never claim/advance, the active home's next eligible deadline.

    In-flight records rely on release wakes. Only deadlines crossed since this tick's
    ungated scan get a bounded rescan. Already-due records were deliberately skipped.
    """
    from cron import jobs, scheduler

    wait = max_wait
    now = jobs._hermes_now().timestamp()
    scanned_at = getattr(_tick_scans, "started", {}).pop(
        hermes_home_key(scheduler._get_hermes_home()), None)
    # Reload after dispatch: workers may have completed and rearmed since reservation selection.
    for job in jobs.load_jobs():
        if (not jobs.is_job_runnable(job) or jobs.is_terminal_job(job)
                or scheduler.is_job_running(job["id"])):
            continue
        due = jobs._parse_aware(job.get("next_run_at"))
        if due is not None:
            remaining = due.timestamp() - now
            if remaining > 0:
                wait = min(wait, remaining)
            elif scanned_at is not None and due > scanned_at:
                wait = min(wait, 1.0)
    return wait


def due_wait_for_homes(max_wait: float, profile_homes=None) -> float:
    """The multiplex ticker sleeps to the earliest deadline in any served home's scope."""
    from cron.scheduler_provider import _profile_cron_scope, _profile_entry

    try:
        if profile_homes is None:
            return next_due_wait(max_wait)
        for entry in profile_homes:
            with _profile_cron_scope(_profile_entry(entry)[1]):
                max_wait = min(max_wait, next_due_wait(max_wait))
    except Exception:
        logger.warning("Cron next-deadline scan failed; retaining heartbeat wait", exc_info=True)
    return max_wait


def begin_tick() -> None:
    # Releases before this tick are already visible to its due scan.
    _wake.clear()
    _tick_scans.started = {}


def wait_for_next_tick(stop_event, timeout, *, max_wait, failures=0, profile_homes=None):
    wait = min(timeout, max_wait)
    if failures:
        # Slot releases must not turn fd-exhaustion backoff into a retry burst.
        stop_event.wait(wait)
        return
    wait = due_wait_for_homes(wait, profile_homes)
    wait_for_tick(stop_event, wait)


def wait_for_tick(stop_event, timeout: float) -> None:
    """Slot releases interrupt the deadline wait. Keep stop_event's shutdown bound small too."""
    if timeout <= 0:
        stop_event.wait(0)
        return
    deadline = time.monotonic() + timeout
    while not stop_event.is_set():
        if _wake.is_set():
            _wake.clear()
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        stop_event.wait(min(remaining, _SLOT_WAKE_CHECK_SECONDS))


def completion_next_run(job: dict, next_run: str | None) -> str | None:
    """Spend scheduling/spawn headroom only for the protected completion cadence."""
    from datetime import timedelta
    from cron.constants import PROTECTED_INTERVAL_HEADROOM_SECONDS

    if next_run is not None and is_protected_interval(job):
        return (datetime.fromisoformat(next_run) - timedelta(
            seconds=PROTECTED_INTERVAL_HEADROOM_SECONDS)).isoformat()
    return next_run
