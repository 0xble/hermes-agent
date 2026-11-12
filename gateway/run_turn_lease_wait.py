"""Durable ownership of inbound turns waiting for a predecessor's transcript flush."""

import asyncio
import logging
import math
import os
import uuid

from gateway.run_pending_recovery import input_already_durable, pending_home_for_key
from gateway.turn_lease import DEFAULT_MAX_LEASE_WAIT
from gateway import shutdown_flush

logger = logging.getLogger("gateway.run")


def lease_wait_max_seconds() -> float:
    """Behavior configuration belongs to the owning profile's YAML, not secrets.

    Never raises: the value only matters after a contended deadline, so a missing, null or
    malformed ``gateway`` block (the shipped template leaves it null) must not fail the turn.
    """
    from gateway.run import _load_gateway_config

    try:
        gateway_cfg = _load_gateway_config().get("gateway")
    except Exception:
        logger.warning("Could not read gateway config, using %s seconds lease wait", DEFAULT_MAX_LEASE_WAIT,
                       exc_info=True)
        return DEFAULT_MAX_LEASE_WAIT
    if gateway_cfg is None:
        return DEFAULT_MAX_LEASE_WAIT
    if not isinstance(gateway_cfg, dict):
        logger.warning("Invalid gateway config section, using %s seconds lease wait", DEFAULT_MAX_LEASE_WAIT)
        return DEFAULT_MAX_LEASE_WAIT
    value = gateway_cfg.get("turn_lease_max_wait_seconds", DEFAULT_MAX_LEASE_WAIT)
    try:
        seconds = float(value)
        if not isinstance(value, bool) and math.isfinite(seconds) and seconds > 0:
            return seconds
    except (TypeError, ValueError):
        pass
    logger.warning("Invalid gateway.turn_lease_max_wait_seconds, using %s seconds", DEFAULT_MAX_LEASE_WAIT)
    return DEFAULT_MAX_LEASE_WAIT


async def lease_wait_notice(runner, session_key, event):
    """Use the existing busy acknowledgement channel, never repeat an event's ack."""
    if (getattr(event, "_gateway_busy_ack_sent", False) or getattr(event, "internal", False)
            or os.environ.get("HERMES_GATEWAY_BUSY_ACK_ENABLED", "true").lower() != "true"):
        return
    adapter = runner._delivery_adapter_for(event.source)
    if adapter is not None:
        from agent.i18n import t

        from agent.notice_style import ContextNotice
        notice = ContextNotice(t(
            "gateway.busy.ack", head=t("gateway.busy.queued_head"), detail="",
            tail=t("gateway.busy.queued_tail")))
        notice._temporary = True
        await runner._send_busy_ack_reply(event, adapter, notice)


async def preserve_lease_wait_async(runner, session_key, session_id, event):
    """Spool failures use the lease's visible fail-closed timeout outcome."""
    write = asyncio.create_task(asyncio.to_thread(
        preserve_lease_wait, runner, session_key, session_id, event,
    ))
    cancelled = None
    try:
        while not write.done():
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError as exc:
                # Cancelling an executor future cannot stop an already-running writer.
                # Publish its spool before the inbound finalizer decides replay ownership.
                cancelled = exc
        await write
    except Exception:
        logger.exception("Cannot preserve turn-lease waiter for %s", session_key)
        if cancelled is None:
            raise TimeoutError from None
    if cancelled is not None:
        raise cancelled


def preserve_lease_wait(runner, session_key, session_id, event):
    """Spool before extending the wait, retaining replay ownership across shutdown."""
    from gateway.run import _profile_runtime_scope

    existing = (getattr(event, "_gateway_lease_wait_spool", None)
                or getattr(event, "_hermes_recovery_spool", None))
    if existing is not None:
        event._gateway_lease_wait_spool = existing
        event._gateway_lease_wait_session_id = session_id
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


def retire_lease_wait(runner, event, *, discard=False):
    """Retain uncommitted input only when shutdown still owns its replay."""
    path = getattr(event, "_gateway_lease_wait_spool", None)
    if path is None:
        return
    identity = getattr(event, "_hermes_recovery_id", None) or event.message_id
    if discard or input_already_durable(runner.session_store, event._gateway_lease_wait_session_id, identity):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not retire turn-lease recovery spool %s", path, exc_info=True)
