"""Durable ownership of inbound turns waiting for a predecessor's transcript flush."""

import logging
import uuid

from gateway.run_pending_recovery import input_already_durable, pending_home_for_key
from gateway import shutdown_flush

logger = logging.getLogger("gateway.run")


def preserve_lease_wait(runner, session_key, session_id, event):
    """Spool before extending the wait. A cancelled waiter remains replayable on startup."""
    from gateway.run import _profile_runtime_scope

    existing = getattr(event, "_gateway_lease_wait_spool", None)
    if existing is not None:
        return
    home = pending_home_for_key(runner, session_key)
    if home is None:
        raise RuntimeError(f"Cannot preserve turn-lease waiter: no served home for {session_key}")
    data = shutdown_flush._serialise_value(event)
    if not data:
        raise RuntimeError("Cannot preserve turn-lease waiter: event is not serializable")
    if not data.get("message_id") and not data.get("recovery_id"):
        data["recovery_id"] = f"hermes-recovery-{uuid.uuid4().hex}"
        event._hermes_recovery_id = data["recovery_id"]
    with _profile_runtime_scope(home, prepared_secret_scope={}):
        path = shutdown_flush._write_payload(shutdown_flush._get_flush_dir(), {
            "session_key": session_key, "reason": "turn_lease_wait",
            "ts": shutdown_flush._pending_arrival_ts(event, 0), "seq": -1, "data": data,
        })
    event._gateway_lease_wait_spool = path
    event._gateway_lease_wait_session_id = session_id
    logger.info("Queued turn for %s behind the held session lease; preserved in %s", session_key, path)


def retire_lease_wait(runner, event):
    """Retire only after the exact input is durable. Otherwise keep the replay copy."""
    path = getattr(event, "_gateway_lease_wait_spool", None)
    if path is None:
        return
    identity = getattr(event, "_hermes_recovery_id", None) or event.message_id
    if input_already_durable(runner.session_store, event._gateway_lease_wait_session_id, identity):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not retire turn-lease recovery spool %s", path, exc_info=True)
