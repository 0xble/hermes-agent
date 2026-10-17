"""Job-level visibility for a recovered, acknowledged worker death."""
import logging

# Logged under the scheduler name so existing log filters and alerts keep matching.
logger = logging.getLogger("cron.scheduler")


def record_external_worker_outcome(
    job: dict, *, error=None, adapters=None, loop=None
) -> bool:
    """Project a detached worker's terminal result onto the job record.

    The worker owns the execution ledger, but the gateway waiter owns the durable
    job-level claim. A hard-wall watchdog can therefore finish the ledger as
    ``failed`` and kill the worker before it reaches ``mark_job_run``. Likewise,
    a dead worker can leave ``unknown``. Fence the normal notice and job update
    together so recovery cannot overwrite a newer fire or duplicate a notice the
    worker already recorded.
    """
    from cron import scheduler
    from cron.unreachable_retry import is_retry_run

    execution = scheduler.get_execution(str(job["execution_id"]))
    if not execution or execution.get("status") not in ("completed", "failed", "unknown"):
        return False
    claim = job.get("fire_claim")
    owner = str(claim.get("by") or "") if isinstance(claim, dict) else ""
    if not owner:
        return False
    with scheduler.fire_claim_fence(job["id"], expected_owner=owner) as owns_claim:
        if not owns_claim:
            return True
        failed = execution.get("status") != "completed"
        error = error or execution.get("error") or "Detached cron worker did not complete."
        delivery_error = None
        if failed:
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
    job: dict, *, error=None, adapters=None, loop=None
) -> bool:
    """Backward-compatible name for the dead-worker recovery path."""
    return record_external_worker_outcome(job, error=error, adapters=adapters, loop=loop)


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
