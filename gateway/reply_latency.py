"""Local receipt-to-delivery records, independent of opt-in shared telemetry.

Keep correlation on the event and its stream consumer, not the chat: multiple
threads and queued turns can be in flight on the same adapter at once.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from gateway.attended_model import is_genuine_user_message
from gateway.platforms.event import turn_origin_for_event

logger = logging.getLogger(__name__)


@dataclass
class ReplyReceipt:
    identity: dict[str, Any]
    started: bool = False
    replied: bool = False


_current: contextvars.ContextVar[ReplyReceipt | None] = contextvars.ContextVar(
    "gateway_reply_receipt", default=None)


def _emit(receipt: ReplyReceipt, event: str, timestamp: float) -> None:
    try:
        logger.info("gateway_reply_latency %s", json.dumps(
            {**receipt.identity, "event": event, "timestamp": timestamp},
            separators=(",", ":"), sort_keys=True))
    except Exception:
        logger.debug("Could not emit reply latency record", exc_info=True)


def stamp_received(event: Any) -> float | None:
    """Preserve the arrival clock before admission, without logging unauthorized input."""
    if not is_genuine_user_message(event):
        return None
    stamp = getattr(event, "_attended_activity_received_at", None)
    if stamp is None:
        stamp = time.time()
        event._attended_activity_received_at = stamp
    return stamp


def record_received(event: Any) -> ReplyReceipt | None:
    """Log an admitted human receipt without letting diagnostics break intake."""
    if not is_genuine_user_message(event):
        return None
    receipt = getattr(event, "_reply_latency_receipt", None)
    if receipt is not None:
        return receipt
    stamp = stamp_received(event)
    try:
        from gateway.outbox import transport_id
        from hermes_cli.profiles import current_profile_name

        source = getattr(event, "source", None)
        if source is None:
            return None
        message_id = getattr(event, "message_id", None)
        thread_id = getattr(source, "thread_id", None)
        receipt = ReplyReceipt({
            # Reuse transport identity already known at receipt. An outbox admission/active
            # token assigned later cannot identify the original queue wait without a new ID.
            "turn_id": transport_id(event),
            "message_id": str(message_id) if message_id is not None else None,
            "platform": str(getattr(source.platform, "value", source.platform)),
            "origin": turn_origin_for_event(event),
            "profile": str(getattr(source, "profile", None) or current_profile_name("default")),
            "chat_id": str(source.chat_id),
            "thread_id": str(thread_id) if thread_id is not None else None,
        })
        event._reply_latency_receipt = receipt
        _emit(receipt, "received", stamp)
        return receipt
    except Exception:
        logger.debug("Could not build reply latency receipt", exc_info=True)
        return None


def bind_agent_start(event: Any) -> None:
    """Bind the admitted event so newly constructed consumers retain its own receipt.

    Event-less interrupt text has no attributable inbound message. Clear the parent
    receipt rather than logging this text as an answer to the chain head.
    """
    receipt = record_received(event)
    _current.set(receipt)  # synthetic turns must clear an inherited human clock
    if receipt is not None and not receipt.started:
        receipt.started = True
        _emit(receipt, "agent_start", time.time())


def current_receipt() -> ReplyReceipt | None:
    return _current.get()


def restore_receipt(receipt: ReplyReceipt | None) -> None:
    """Restore a recursive turn's parent without emitting another agent_start."""
    _current.set(receipt)


def record_first_reply(receipt: ReplyReceipt | None, text: str, result: Any = None) -> None:
    """Call only at a confirmed transport success, never a journal/optimistic mark."""
    if receipt is None or receipt.replied or not text.strip():
        return
    if result is not None:
        raw = getattr(result, "raw_response", None)
        if getattr(result, "success", False) is not True or (isinstance(raw, dict) and raw.get("skipped")):
            return
    receipt.replied = True
    _emit(receipt, "first_reply", time.time())
