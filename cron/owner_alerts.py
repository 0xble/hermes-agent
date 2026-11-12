"""Durable owner notices for scheduler failures that normal/local lanes must not hide."""


def queue_owner_failure(job: dict, receipt: str, content: str, *, output_file=None) -> None:
    """Queue once per caller-supplied occurrence identity via the cron failure route.

    The outbox keeps attempted-send tombstones, including after restart. A caller must
    commit this receipt BEFORE consuming its occurrence from jobs.json. Transport
    delivery is at-most-once (not a claim of exactly-once remote acknowledgement).
    """
    from cron import delivery_queue
    from cron.scheduler import _upsert_incident_for_failure

    notice = dict(job)
    for key in ("review", "output_local_only", "failure_deliver", "execution_id"):
        notice.pop(key, None)
    notice["deliver"] = "origin"
    _withheld, incident_id = _upsert_incident_for_failure(
        job, content, output_file=output_file)
    if incident_id:
        notice["_failure_incident_id"] = incident_id
    # Do not use incident cooldown to suppress a different missed occurrence.
    delivery_queue.enqueue(receipt, notice, content, for_failure=True, terminal_gate=False)
