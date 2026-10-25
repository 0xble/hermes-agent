"""Job-level visibility for a recovered, acknowledged worker death."""
import logging

# Logged under the scheduler name so existing log filters and alerts keep matching.
logger = logging.getLogger("cron.scheduler")


def record_external_worker_outcome(
    job: dict, *, error=None, adapters=None, loop=None, worker_exited: bool = False,
    worker_terminated: bool = False, worker_exit_code: int | None = None,
    execution_id: str | None = None, expected_fire_owner: str | None = None,
) -> bool:
    """Project a detached worker's terminal result onto the job record.

    The worker owns the execution ledger, but the gateway waiter owns the durable
    job-level claim. A terminal ledger row is not enough while the worker is still
    alive: it commits before delivery and before its final job bookkeeping. The
    scheduler waiter therefore passes ``worker_exited`` only after the process has
    been reaped. Watchdog failures and unknown outcomes are the only rows whose
    failure notice is known to be unsent; ordinary worker results are projected
    without sending a second crash notice.
    """
    from cron import scheduler
    from cron.unreachable_retry import is_retry_run

    execution_key = execution_id or job.get("execution_id")
    if not execution_key:
        logger.error("Job '%s': cannot reconcile worker outcome without execution id", job.get("id"))
        return False
    execution = scheduler.get_execution(str(execution_key))
    status = execution.get("status") if execution else None
    if not execution or status not in ("completed", "failed", "unknown"):
        return False
    if status in ("completed", "failed") and not worker_exited:
        # A terminal row can precede delivery in a live worker. Never race it
        # from a direct recovery call; the waiter will reconcile after exit.
        return False
    claim = job.get("fire_claim")
    owner = expected_fire_owner if expected_fire_owner is not None else (
        str(claim.get("by") or "") if isinstance(claim, dict) else ""
    )
    if not owner:
        return False
    with scheduler.fire_claim_fence(job["id"], expected_owner=owner) as owns_claim:
        if not owns_claim:
            return True
        nonzero_worker_exit = worker_exit_code not in (None, 0)
        failed = status != "completed" or worker_terminated or nonzero_worker_exit
        execution_error = execution.get("error")
        if status == "failed":
            # A failed worker records its authoritative failure before exiting. Prefer that
            # durable cause over the waiter's generic exit-status wrapper.
            error = execution_error or error or "Detached cron worker did not complete."
        else:
            # For unknown outcomes the waiter may have captured the worker's stderr. Keep that
            # evidence rather than replacing it with the generic dead-owner text.
            error = error or execution_error or "Detached cron worker did not complete."
        # A watchdog has already terminalized the row and then exits 124. Its
        # durable error marker is the authoritative proof that no worker-owned
        # failure notice was sent. Normal worker failures may already have sent
        # their own notice (and can carry a delivered queue status), so never
        # synthesize a crash notice for them.
        from cron.scheduler_detached_worker import HARD_WALL_TIMEOUT_ERROR_PREFIX

        watchdog_failure = (
            status == "failed"
            and str(execution.get("error") or "").startswith(
                HARD_WALL_TIMEOUT_ERROR_PREFIX
            )
        )
        delivery_error = None
        post_commit_worker_failure = status == "completed" and nonzero_worker_exit
        if status == "unknown" or watchdog_failure or worker_terminated or post_commit_worker_failure:
            scheduler.logger.error("Job '%s': %s", job["id"], error)
            scope_tokens = scheduler._install_fire_secret_scope()
            try:
                delivery_error, _ = scheduler._deliver_crash_failure(
                    job, error, adapters=adapters, loop=loop
                )
            finally:
                scheduler._reset_fire_secret_scope(scope_tokens)
        scheduler.mark_job_run(
            job["id"],
            success=not failed,
            error=None if not failed else error,
            delivery_error=delivery_error,
            expected_fire_owner=owner,
            # A ladder re-run's occurrence already counted toward repeat.
            **({"ladder_rung": True} if is_retry_run(job) else {}),
        )
    return True


def record_unknown_worker_outcome(
    job: dict, *, error=None, adapters=None, loop=None, worker_terminated: bool = False,
    worker_exit_code: int | None = None,
) -> bool:
    """Backward-compatible name for the dead-worker recovery path."""
    return record_external_worker_outcome(
        job,
        error=error,
        adapters=adapters,
        loop=loop,
        worker_exited=True,
        worker_terminated=worker_terminated,
        worker_exit_code=worker_exit_code,
    )


def external_worker_exited_reason(returncode: int | None) -> str:
    """Cause for an attempt whose restart-safe worker this process watched exit.

    The generic dead-owner sweep can only say the owner vanished, so it names a
    scheduler restart — but this waiter never lost its scheduler: it handed the
    attempt to the external worker and then saw that worker exit with this status.
    The run is still ``unknown`` rather than ``failed`` (whether side effects ran is
    still unknown); only the cause becomes truthful (#128509).
    """
    return (
        f"Restart-safe cron worker exited with status {returncode} after adopting this "
        "execution, without writing a durable terminal state; whether side effects ran "
        "is unknown."
    )


def finish_interrupted_run(job: dict, execution_id: str, delivery_error) -> None:
    """Shutdown already wrote last_status, so mark_job_run is skipped (a second call would skip a
    fire or auto-delete the job); an unsent notice is recorded via update_job instead."""
    from cron import scheduler
    if delivery_error:
        try:
            # The gateway shutdown already wrote last_status for this run, so mark_job_run is skipped below
            # — but it could not know that the notice we just tried to send never left the process (the
            # adapters were torn down first, #82232). Record the delivery failure on its own via update_job:
            # mark_job_run also advances next_run_at and the repeat counter, and running that a second time
            # for one run would skip a fire or auto-delete the job early.
            from cron.jobs import update_job
            update_job(job["id"], {"last_delivery_error": delivery_error})
        except Exception as _rec_err:
            logger.debug(
                "Failed recording delivery_error for interrupted job %s: %s", job["id"], _rec_err, exc_info=True)
    scheduler.finish_execution(
        execution_id, success=False,
        error="Run stopped before finishing; no terminal result was recorded.")
