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
_SLOT_WAKE_CHECK_SECONDS = 0.25


def reserved_job_id(jobs: list[dict], home) -> str | None:
    """Select in store order, not due order, so a later deadline cannot steal the reservation."""
    eligible = tuple(str(job["id"]) for job in jobs if is_protected_interval(job))
    key = hermes_home_key(home)
    if _loaded_protected.get(key) != eligible:
        _loaded_protected[key] = eligible
        if len(eligible) > 1:
            logger.error(
                "Cron has %d protected interval jobs but only 1 reserved worker; "
                "%s use the general pool best-effort (FIFO)", len(eligible), ", ".join(eligible[1:]))
    return eligible[0] if eligible else None


def submit_due_jobs(due_jobs: list[dict], pool, process_job) -> list:
    """Preserve general FIFO while sending only the selected protected job to its reserve."""
    from cron import jobs, scheduler

    reserved_id = reserved_job_id(jobs.load_jobs(), scheduler._get_hermes_home())
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
        future = scheduler._submit_with_guard(job, pool, process_job)
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


def worker_released(inflight_key: tuple) -> None:
    """Called only AFTER release_running_job has passed the detached-process exit fence."""
    with _lock:
        claim = _reserved_claims.get(inflight_key[0])
        if claim is not None and claim[0] == inflight_key[1]:
            _reserved_claims.pop(inflight_key[0])
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


def next_due_wait(max_wait: float) -> float:
    """Read, never claim/advance, the active home's next eligible deadline.

    In-flight or already-due records cannot shorten the wait: their release wakes us,
    while failed/paused admission must not spin at zero delay.
    """
    from cron import jobs, scheduler

    wait = max_wait
    now = jobs._hermes_now().timestamp()
    for job in jobs.load_jobs():
        if (not job.get("enabled", True) or jobs._has_pause_marker(job)
                or jobs.is_terminal_job(job) or scheduler.is_job_running(job["id"])):
            continue
        due = jobs._parse_aware(job.get("next_run_at"))
        if due is not None and due.timestamp() > now:
            wait = min(wait, due.timestamp() - now)
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


def _inflight_min_allowance_minutes() -> float:
    """Stale allowance floor (min): ``cron.inflight_max_minutes``, else env escape hatch/default."""
    import contextlib
    from cron import scheduler as _sched

    with contextlib.suppress(Exception):
        _ucfg = _sched.load_config() or {}
        _cfg_val = (
            _ucfg.get("cron", {}) if isinstance(_ucfg, dict) else {}
        ).get("inflight_max_minutes")
        if _cfg_val is not None:
            val = float(_cfg_val)
            if val > 0:
                return val
    raw = _sched.cron_env_setting("HERMES_CRON_INFLIGHT_MAX_MINUTES").strip()
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except (ValueError, TypeError):
            _sched.logger.warning(
                "Invalid HERMES_CRON_INFLIGHT_MAX_MINUTES=%r; using default %s",
                raw,
                _sched._INFLIGHT_MIN_ALLOWANCE_MINUTES)
    return _sched._INFLIGHT_MIN_ALLOWANCE_MINUTES


def completion_next_run(job: dict, next_run: str | None) -> str | None:
    """Spend scheduling/spawn headroom only for the protected completion cadence."""
    from datetime import timedelta
    from cron.constants import PROTECTED_INTERVAL_HEADROOM_SECONDS

    if next_run is not None and is_protected_interval(job):
        return (datetime.fromisoformat(next_run) - timedelta(
            seconds=PROTECTED_INTERVAL_HEADROOM_SECONDS)).isoformat()
    return next_run
