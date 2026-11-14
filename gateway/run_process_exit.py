"""Process-only gateway exit, before asyncio.Runner joins abandoned workers."""
from __future__ import annotations

import asyncio
import os
import sys
from contextvars import ContextVar
from contextlib import suppress


_PROCESS_EXIT_STATE: ContextVar[dict | None] = ContextVar(
    "gateway_process_exit_state", default=None
)


def current_process_exit_state() -> dict | None:
    """Return the process-entry state collected by the active exit wrapper."""
    return _PROCESS_EXIT_STATE.get()


async def run_gateway_process(start_gateway, *args, before_exit=None, **kwargs):
    """Await the entire durable teardown, then use the existing hard-exit path.

    asyncio.run() closes its default executor AFTER the main coroutine returns.
    A cancelled to_thread cleanup can still be blocked in filesystem/network I/O;
    placing os._exit after asyncio.run therefore never reaches it. Embedded
    start_gateway callers keep their normal coroutine lifecycle instead.
    """
    from gateway.run import _exit_after_graceful_shutdown

    success = None
    error = None
    hard_exit = True
    state_token = _PROCESS_EXIT_STATE.set({})
    try:
        try:
            success = await start_gateway(*args, **kwargs)
        except SystemExit as exc:
            error = exc
            code = 0 if exc.code is None else exc.code if isinstance(exc.code, int) else 1
        except KeyboardInterrupt as exc:
            error = exc
            code = 0
        except asyncio.CancelledError as exc:
            # A cancellation is clean only when the runner recorded a requested stop.
            # Otherwise launchd must see a failure and relaunch the gateway.
            error = exc
            state = current_process_exit_state() or {}
            requested = bool(state.get("shutdown_requested"))
            requested_code = state.get("exit_code")
            code = requested_code if requested and isinstance(requested_code, int) else int(not requested)
        except Exception as exc:  # health: allow BLE001 -- preserve ordinary gateway failure after diagnostics
            # Preserve the diagnostic hook for ordinary exceptions, while retaining the
            # existing propagation path instead of hiding the original failure. The
            # BaseException paths above still use the hard-exit backstop.
            error = exc
            code = 1
            hard_exit = False
        else:
            code = 0 if success else 1
        if before_exit is not None:
            before_exit(code, success, error)
        if hard_exit:
            _exit_after_graceful_shutdown(code)
        if error is not None:
            raise error.with_traceback(error.__traceback__)
        return success  # Test doubles may replace os._exit.
    finally:
        _PROCESS_EXIT_STATE.reset(state_token)


def exit_after_graceful_shutdown(exit_code: int) -> None:
    """Flush stdio, release the PID file + lock, stamp the clean exit, flush opt-in metrics, hard-exit.
    ``os._exit`` (not ``sys.exit``): SystemExit runs ``Py_FinalizeEx``, which joins every non-daemon
    thread — exactly the hang a wedged worker causes. It bypasses ``atexit``, so the metrics flush,
    PID/lock release and the bounded log drain (file handlers sit behind a ``QueueListener`` thread)
    are done here explicitly; graceful teardown is already complete by the time this runs. See #53107.
    The atexit-registered ``remove_pid_file`` / ``release_gateway_runtime_lock`` (``start_gateway``) never
    run either. ``_stop_impl`` releases both on the full shutdown, but the EARLY exits — clean-fatal-config
    (#51228) and startup-aborted-before-running — raise ``SystemExit`` right after ``runner.start()``
    without ``_stop_impl``, so atexit was all that released them there; this backstop does now (#53107).
    Both are idempotent (``remove_pid_file`` only unlinks this process's PID file; the lock release no-ops
    once released): a no-op on the normal shutdown, the actual cleanup on the early exits.
    """
    from gateway.run import _best_effort

    for stream in (sys.stdout, sys.stderr):
        with suppress(Exception):
            stream.flush()
    def _release_locks() -> None:
        # BEFORE the log drain (bounded, but could take its full timeout on a wedged disk); idempotent.
        from gateway.status import remove_pid_file, release_gateway_runtime_lock
        remove_pid_file()
        release_gateway_runtime_lock()

    def _mark_exited() -> None:
        from gateway.zero_cut_standby import is_standby
        if is_standby():
            return
        # Single funnel every graceful exit passes through, so the next boot's unclean-death detector
        # fires only for genuine SIGKILL/OOM/VM deaths. Ownership-guarded against an old --replace life.
        from hermes_cli.observability.shared_metrics_process import stamp_exit
        stamp_exit("clean")  # the exit-metrics marker too: os._exit skips its atexit stamp (never raises)
        from gateway.lifecycle_ledger import mark_exited
        mark_exited(exit_code, reason="graceful_shutdown")

    def _flush_metrics() -> None:  # after the clean stamp: a supervisor kill mid-flush is no unclean death
        from hermes_cli.observability.shared_metrics_process import flush_before_hard_exit
        flush_before_hard_exit()  # the runtimes' atexit flush: open sessions + queued opt-in rows (bounded)

    def _write_cpu_profile() -> None:  # --cpu-profile's atexit writer never runs past os._exit; no-op when off
        from hermes_cli.cpu_profile import write_cpu_profile
        write_cpu_profile()

    def _drain_logs() -> None:
        # os._exit bypasses the listener's atexit drain. Bounded, no restart — NOT flush_log_queue():
        # a listener wedged on the rotation lock would re-freeze shutdown in an unbounded stop() join.
        from hermes_logging import drain_log_queue
        drain_log_queue(timeout=1.0)

    for _step in (_release_locks, _mark_exited, _flush_metrics, _write_cpu_profile, _drain_logs):
        _best_effort(_step)
    os._exit(exit_code)
