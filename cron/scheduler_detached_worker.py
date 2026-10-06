"""Cron: teardown of a worker that outlived its ``run_job``.

``ThreadPoolExecutor.shutdown(wait=False)`` after an inactivity timeout does not stop a
worker already inside ``run_conversation``. Finalizing its SessionDB from ``run_job``'s
``finally`` would close a handle the worker is still writing to — the checkpoint/WAL-unlink
overlap behind #102827. The worker's Future owns the teardown instead.
"""

from __future__ import annotations

import concurrent.futures
import math
import os
import subprocess
import threading
from typing import Optional


def hard_wall_timeout_seconds(job: Optional[dict] = None) -> float:
    """Job override or finite profile bound, separate from inactivity/script limits."""
    from cron.jobs import _normalize_hard_wall_timeout

    if job:
        try:
            override = _normalize_hard_wall_timeout(job.get("hard_wall_timeout_seconds"))
            if override is not None:
                return override
        except ValueError:
            pass  # Malformed legacy payloads retain the finite profile safety cap.
    from cron.scheduler import load_config_readonly
    try:
        value = float((load_config_readonly().get("cron") or {}).get("hard_wall_timeout_seconds", 7200))
        return value if math.isfinite(value) and value > 0 else 7200.0
    except (TypeError, ValueError, OSError):
        return 7200.0


def _terminate_owned_descendants(
    pid: int, started_at: int, execution_id: Optional[str] = None, *, orphan_only: bool = False,
) -> bool:
    """Sweep the owner tree and same-user processes carrying its exact execution marker.

    With orphan_only, only marker-matched processes outside the current tree are
    selected. Script teardown already handled that tree, and other worker children
    must survive the script's own timeout.

    A double-forked setsid child is reparented before a tree snapshot. The
    inherited execution marker preserves ownership across that boundary;
    require both matching marker and a process created after this worker.
    """
    import psutil
    from cron.executions import _owner_identity

    if _owner_identity(pid, started_at) != "live":
        return False
    try:
        parent = psutil.Process(pid)
        descendants = parent.children(recursive=True)
        children = [] if orphan_only else descendants
        if execution_id:
            owner_uid = parent.uids().real
            owner_start = parent.create_time()
            known = {child.pid for child in descendants}
            for candidate in psutil.process_iter():
                try:
                    if (candidate.pid in known or candidate.pid == pid
                            or candidate.uids().real != owner_uid
                            or candidate.create_time() < owner_start
                            or candidate.environ().get("_HERMES_CRON_EXTERNAL_WORKER") != execution_id):
                        continue
                    children.append(candidate)
                    known.add(candidate.pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    for child in reversed(children):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            return False
    _, alive = psutil.wait_procs(children, timeout=1)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            return False
    _, alive = psutil.wait_procs(alive, timeout=2)
    return not alive


class HardWallFence:
    """Lifetime timer only; the SQLite execution row is the sole result fence."""

    def __init__(self) -> None:
        self.stopped = threading.Event()

    def set(self) -> None:
        self.stopped.set()


def arm_hard_wall_timeout(execution_id: str, profile_home, seconds: float) -> HardWallFence:
    """Watchdog lives inside the detached worker, not the replaceable gateway."""
    from pathlib import Path
    from cron.executions import _owner_identity, _process_start_time, finish_execution, get_execution
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    fence = HardWallFence()
    pid = os.getpid()
    fingerprint = _process_start_time(pid)
    if fingerprint is None:
        raise RuntimeError("Detached cron worker has no verifiable start-time fingerprint")

    def expire() -> None:
        if fence.stopped.wait(seconds):
            return
        home_token = set_hermes_home_override(Path(profile_home))
        try:
            timeout_won = False
            try:
                if _owner_identity(pid, fingerprint) == "live":
                    timeout_won = finish_execution(
                        execution_id, success=False,
                        error=f"Detached cron run exceeded hard wall-clock timeout ({seconds:g}s).",
                        require_running=True,
                    ) is not None
                # Reserve the final 3s for bounded descendant cleanup. The
                # post-commit allowance is derived from the configured wall cap,
                # with enough headroom to include cleanup inside cap+grace.
                if not timeout_won:
                    grace = min(60.0, max(4.0, seconds))
                    fence.stopped.wait(grace - 3.0)
            finally:
                try:
                    _terminate_owned_descendants(pid, fingerprint, execution_id)
                finally:
                    exit_code = 124 if timeout_won else 1
                    if not timeout_won:
                        try:
                            record = get_execution(execution_id)
                            if record and record["status"] == "completed":
                                exit_code = 0
                            elif record and record["status"] == "running":
                                # Database errors on the first claim must not strand
                                # the row while the worker is being killed.
                                finish_execution(
                                    execution_id, success=False,
                                    error="Detached cron worker could not complete before termination.",
                                    require_running=True)
                        except Exception:
                            pass
                    os._exit(exit_code)
        finally:
            reset_hermes_home_override(home_token)

    threading.Thread(target=expire, name=f"cron-hard-wall-{execution_id}", daemon=True).start()
    return fence


def defer_teardown_to_running_worker(
    future: Optional[concurrent.futures.Future], session_db, agent, job_id: str, job_name: str,
    cron_session_id: str,
) -> bool:
    """Return True when the worker is still running and its Future will finalize the session
    and tear the agent down on completion; False when the caller must do it now."""
    if future is None or future.done():
        return False
    from cron.scheduler import _finalize_cron_session, _teardown_cron_agent

    def _finish(_future) -> None:
        try:
            if session_db:
                _finalize_cron_session(session_db, agent, job_id, job_name, cron_session_id)
        finally:
            _teardown_cron_agent(agent, job_id)

    # Runs inline if the worker finished between done() and here — still exactly once.
    future.add_done_callback(_finish)
    return True


def reap_terminal_worker_in_background(process: subprocess.Popen) -> None:
    """Keep the reap contract when the waiter returns before the worker exits.

    The ledger turning terminal lets ``_wait_for_external_cron_worker_body``
    return while the worker is still in final teardown. The gateway remains the
    worker's parent, so if nobody calls ``wait()`` afterwards the worker lingers
    as a zombie (STAT=Z) under the gateway until it is restarted (#114509). A
    short-lived daemon thread holds that single responsibility and ends with
    the process exit it waits for.
    """
    threading.Thread(
        target=process.wait,
        name=f"cron-worker-reap-{getattr(process, 'pid', '?')}",
        daemon=True,
    ).start()
