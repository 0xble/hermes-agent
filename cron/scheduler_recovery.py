"""Incident projection and notification for recovered cron attempts."""

from typing import Optional


def _record_reclaimed_delivery_outcome(record: dict, outcome: str) -> None:
    from cron import scheduler

    try:
        from cron.executions import record_execution_delivery_outcome

        record_execution_delivery_outcome(record["id"], outcome)
    except Exception as exc:
        scheduler.logger.debug(
            "Failed recording reclaimed execution %s delivery outcome: %s",
            record.get("id", "?"), exc, exc_info=True)


def _alert_reclaimed_execution(record: dict, *, adapters=None, loop=None) -> None:
    """Persist one reclaimed attempt and notify only when its owner is known dead."""
    from cron import scheduler

    outcome = "failed"
    try:
        from cron.jobs import get_job

        job = get_job(record["job_id"])
        if job is None:
            raise LookupError(f"cron job {record['job_id']} no longer exists")
    except Exception as exc:
        scheduler.logger.debug(
            "Job lookup failed for reclaimed execution %s: %s", record.get("id", "?"), exc,
            exc_info=True)
        _record_reclaimed_delivery_outcome(record, outcome)
        return

    from cron.jobs import _under_fire_fence

    def notify_if_pending():
        # The waiter can observe the same unknown row before this sweep delivers.
        # Both lanes use this job's fire fence and re-read the per-attempt disposition.
        current = scheduler.get_execution(record["id"])
        if current and current.get("delivery_outcome") is not None:
            return
        with scheduler._fire_secret_scope():
            _deliver_reclaimed_execution(record, job, adapters=adapters, loop=loop)

    _under_fire_fence(job["id"], notify_if_pending)


def _deliver_reclaimed_execution(record: dict, job: dict, *, adapters=None, loop=None) -> None:
    """Deliver under the owning profile's secret scope and the shared fire fence."""
    from cron import scheduler

    error = str(record.get("error") or "Cron execution owner exited before completion.")
    incident_acked, incident_id = scheduler._upsert_incident_for_failure(job, error)
    if incident_acked:
        _record_reclaimed_delivery_outcome(record, "suppressed_acked")
        return

    from cron.executions import _OWNER_WEDGED_REASON

    if error == _OWNER_WEDGED_REASON:
        # A live-but-stale owner is a durable incident worth surfacing in the
        # ledger, but is not the confirmed-death alert this PR owns.
        _record_reclaimed_delivery_outcome(record, "incident_only_wedged")
        return

    normalized_deliver = scheduler._normalize_deliver_value(scheduler._delivery_lane_value(job, for_failure=True))
    delivery_error = None
    try:
        delivery_error = scheduler._deliver_result(
            job,
            scheduler._summarize_cron_failure_for_delivery(job, error),
            adapters=adapters,
            loop=loop,
            for_failure=True,
        )
    except Exception as exc:
        delivery_error = str(exc)
        scheduler.logger.error("Delivery failed for reclaimed cron execution %s: %s", record["id"], exc,
                     exc_info=True)
    unresolved_origin = bool(
        not delivery_error
        and normalized_deliver == "origin"
        and not scheduler._resolve_delivery_targets(job, for_failure=True)
    )
    outcome = scheduler._classify_delivery_outcome(
        delivery_error=delivery_error,
        delivery_queued=job.get("last_delivery_queued"),
        should_deliver=True,
        unresolved_origin=unresolved_origin,
        normalized_deliver=normalized_deliver,
        incident_acked=False,
        success=False,
        notification_suppressed=bool(job.get("_notification_all_targets_suppressed")),
    )
    if outcome == "delivered":
        scheduler._mark_incident_alerted(incident_id)
    _record_reclaimed_delivery_outcome(record, outcome)


def _alert_reclaimed_executions(
    reclaimed, *, adapters=None, loop=None, exclude_execution_id: Optional[str] = None
) -> int:
    from cron import scheduler

    for record in getattr(reclaimed, "records", ()):
        if record.get("id") == exclude_execution_id:
            continue
        try:
            _alert_reclaimed_execution(record, adapters=adapters, loop=loop)
        except Exception as exc:
            scheduler.logger.debug(
                "Alerting failed for reclaimed execution %s: %s", record.get("id", "?"), exc,
                exc_info=True)
    return int(reclaimed)


def _recover_interrupted_executions_with_alerts(*, adapters=None, loop=None) -> int:
    from cron.executions import recover_interrupted_executions as recover

    return _alert_reclaimed_executions(recover(), adapters=adapters, loop=loop)


