"""Bounded session-store acquisition for persisted cron agent runs."""

import concurrent.futures
import contextvars
import logging
import time
from pathlib import Path

logger = logging.getLogger("cron.scheduler")


class CronSessionStoreUnavailable(RuntimeError):
    """The cron session store could not be opened, so the agent must not start."""


_CRON_SESSION_DB_ATTEMPTS = 3
_CRON_SESSION_DB_RETRY_BACKOFF = 0.05


def _open_cron_session_db(job: dict):
    """Open the SQLite session store with bounded retries before an agent can be constructed.

    HERMES_CRON_TIMEOUT only watches ``run_conversation``. A wedged sqlite3.connect is retried
    briefly and then fails the cron run explicitly, never starting an agent without persistence.
    """
    from cron.scheduler import _get_session_db_timeout

    _session_db_timeout = _get_session_db_timeout()
    attempts = _CRON_SESSION_DB_ATTEMPTS if _session_db_timeout > 0 else 1
    last_error = None
    from hermes_state_registry import acquire, release_or_close
    from gateway.session_cron import current_execution

    owner = current_execution()
    from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError, is_worker_process
    if owner is not None and isinstance(owner[0].db, RuntimeSessionStore):
        return owner[0].db
    if owner is None and is_worker_process():
        # Worker assignments must bind their canonical owner store before running billed inference.
        raise WorkerPersistenceError("worker_cron_registration_required")

    def acquire_store():
        if owner is None:
            return acquire()
        db = acquire(Path(owner[0].db.db_path))
        if db is not owner[0].db:
            release_or_close(db)
            raise RuntimeError("cron requires the canonical owner store")
        return db

    for attempt in range(1, attempts + 1):
        try:
            if _session_db_timeout <= 0:
                handle = acquire_store()
                if handle is not None:
                    return handle
                break
            _session_db_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            # Copy the context so a profile run resolves ITS OWN home/state.db on the worker thread
            # instead of the process-global default.
            _session_db_context = contextvars.copy_context()
            _session_db_future = _session_db_pool.submit(
                _session_db_context.run, acquire_store
            )
            try:
                handle = _session_db_future.result(timeout=_session_db_timeout)
                if handle is not None:
                    return handle
            except concurrent.futures.TimeoutError as exc:
                # The abandoned worker may still finish; close its late result or its SQLite FDs leak.
                # The worker is abandoned (shutdown below doesn't wait for it). If SessionDB() later completes
                # inside it, the future's result would be orphaned and its SQLite FDs (.db, WAL, SHM) leak until
                # process exit. Register a done-callback that retrieves and closes any eventual late result
                # (#72782).
                from cron.scheduler_detached_worker import _close_late_session_db_result

                _session_db_future.add_done_callback(_close_late_session_db_result)
                last_error = exc
                logger.warning(
                    "Job '%s': SessionDB init attempt %d/%d timed out after %.1fs",
                    job.get("id", "?"),
                    attempt,
                    attempts,
                    _session_db_timeout,
                )
            finally:
                # Abandon a wedged connect() rather than blocking shutdown on it.
                _session_db_pool.shutdown(wait=False)
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Job '%s': SessionDB init attempt %d/%d failed: %s",
                job.get("id", "?"),
                attempt,
                attempts,
                exc,
                exc_info=True,
            )
        if attempt < attempts:
            time.sleep(_CRON_SESSION_DB_RETRY_BACKOFF)

    raise CronSessionStoreUnavailable(
        "session store unavailable; run not started"
    ) from last_error
