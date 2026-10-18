"""Bounded exit proof and failure classification for restart-safe cron workers."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Optional


TERMINAL_EXIT_GRACE_SECONDS = 60.0


class ExternalWorkerExitTimeout(RuntimeError):
    """A terminal worker did not exit within the bounded parent-side grace period."""

    def __init__(
        self,
        message: str,
        *,
        worker_terminated: bool = False,
        worker_exit_code: int | None = None,
    ):
        super().__init__(message)
        self.worker_terminated = worker_terminated
        self.worker_exit_code = worker_exit_code


def _raise_bad_exit(returncode: int, message: str, *, terminated: bool = False) -> None:
    if returncode < 0:
        raise ExternalWorkerExitTimeout(
            f"Restart-safe cron worker was terminated with status {returncode} {message}",
            worker_terminated=True,
            worker_exit_code=returncode,
        )
    if returncode:
        raise ExternalWorkerExitTimeout(
            f"Restart-safe cron worker exited with status {returncode} {message}",
            worker_terminated=terminated,
            worker_exit_code=returncode,
        )


def _force_terminal_worker_exit(process: subprocess.Popen) -> int | None:
    returncode = process.poll()
    if returncode is not None:
        return returncode
    for stop in (process.terminate, process.kill):
        try:
            stop()
        except (OSError, ProcessLookupError):
            pass
        try:
            returncode = process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            returncode = process.poll()
        if returncode is not None:
            break
    return returncode


def _finish_terminal_deadline(process: subprocess.Popen, *, wait_for_process_exit: bool) -> bool:
    returncode = process.poll()
    if returncode is None and wait_for_process_exit:
        returncode = _force_terminal_worker_exit(process)
        if returncode is None:
            raise ExternalWorkerExitTimeout(
                "Restart-safe cron worker stayed alive after its terminal exit grace period; "
                "execution was not projected.",
                worker_terminated=True,
            )
        _raise_bad_exit(returncode, "after its terminal exit grace period.", terminated=returncode < 0)
        return True
    if returncode is None:
        from cron.scheduler_detached_worker import reap_terminal_worker_in_background

        reap_terminal_worker_in_background(process)
        return True
    _raise_bad_exit(returncode, "after its terminal exit grace period.")
    return True


def _terminal_timeout_step(
    process: subprocess.Popen,
    *,
    wait_for_process_exit: bool,
) -> bool:
    returncode = process.poll()
    if returncode is not None:
        _raise_bad_exit(returncode, "after terminalizing its execution.")
        return True
    if not wait_for_process_exit:
        from cron.scheduler_detached_worker import reap_terminal_worker_in_background

        reap_terminal_worker_in_background(process)
        return True
    return False


def _terminal_execution_result(
    scheduler,
    execution_id: str,
    returncode: int,
    *,
    wait_for_process_exit: bool,
) -> bool:
    current = scheduler.get_execution(execution_id)
    if not current or current.get("status") not in ("completed", "failed"):
        return False
    if returncode is not None and returncode < 0 and wait_for_process_exit:
        raise ExternalWorkerExitTimeout(
            f"Restart-safe cron worker was terminated with status {returncode} "
            "after its terminal exit grace period.",
            worker_terminated=True,
            worker_exit_code=returncode,
        )
    if returncode not in (None, 0):
        _raise_bad_exit(returncode, "after terminalizing its execution.")
    return True


def _recover_dead_worker(scheduler, execution_id: str, returncode: int, stderr_path: Optional[Path]) -> bool:
    from cron.scheduler_worker_failure import external_worker_exited_reason

    reason = external_worker_exited_reason(returncode)
    if not scheduler.terminalize_dead_owner(execution_id, reason=reason):
        scheduler.recover_interrupted_executions()
        current = scheduler.get_execution(execution_id)
        if current and current.get("status") in ("completed", "failed"):
            return True
    stderr_tail = ""
    if stderr_path is not None:
        from cron.scheduler_diagnostics import external_worker_stderr_tail

        stderr_tail = external_worker_stderr_tail(stderr_path)
    raise RuntimeError(f"{reason}{stderr_tail}")


def wait_external_worker_body(
    process: subprocess.Popen,
    *,
    execution_id: str,
    stderr_path: Optional[Path] = None,
    wait_for_process_exit: bool = False,
    terminal_exit_grace_seconds: float = TERMINAL_EXIT_GRACE_SECONDS,
    worker_hard_wall_deadline: Optional[float] = None,
) -> bool:
    """Wait for a restart-safe worker without projecting a live terminal row."""
    from cron import scheduler

    def is_terminal() -> bool:
        current = scheduler.get_execution(execution_id)
        return bool(current and current.get("status") in scheduler._TERMINAL_STATES)

    terminal_deadline = None
    while True:
        timeout = 1.0
        if terminal_deadline is not None:
            remaining = terminal_deadline - time.monotonic()
            if remaining <= 0:
                return _finish_terminal_deadline(process, wait_for_process_exit=wait_for_process_exit)
            timeout = min(timeout, max(0.001, remaining))
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if not is_terminal():
                continue
            if terminal_deadline is None:
                terminal_base = max(time.monotonic(), worker_hard_wall_deadline) if worker_hard_wall_deadline is not None else time.monotonic()
                terminal_deadline = terminal_base + max(0.0, float(terminal_exit_grace_seconds))
            if _terminal_timeout_step(process, wait_for_process_exit=wait_for_process_exit):
                return True
            continue

        if _terminal_execution_result(scheduler, execution_id, returncode, wait_for_process_exit=wait_for_process_exit):
            return True
        if _recover_dead_worker(scheduler, execution_id, returncode, stderr_path):
            return True
