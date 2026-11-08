"""Local receipt-to-delivery records, independent of opt-in shared telemetry.

Keep correlation on the event and its stream consumer, not the chat: multiple
threads and queued turns can be in flight on the same adapter at once.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from gateway.attended_model import is_genuine_user_message
from gateway.platforms.event import turn_origin_for_event

logger = logging.getLogger(__name__)


@dataclass
class ReplyReceipt:
    identity: dict[str, Any]
    started: bool = False
    replied: bool = False
    admitted: bool = False
    last_delivery_at: float | None = None
    last_delivery_kind: str | None = None
    last_delivery_text: str = ""
    log_dir: Path | None = None


_current: contextvars.ContextVar[ReplyReceipt | None] = contextvars.ContextVar(
    "gateway_reply_receipt", default=None)
_delivery_kind: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "gateway_reply_delivery_kind", default=None)


@lru_cache(maxsize=128)
def _ensure_log_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)


def _emit(receipt: ReplyReceipt, event: str, timestamp: float, **details: Any) -> None:
    try:
        payload = json.dumps(
            {**receipt.identity, "event": event, "timestamp": timestamp, **details},
            separators=(",", ":"), sort_keys=True)
        logger.info("gateway_reply_latency %s", payload)
        from hermes_constants import get_hermes_home
        directory = receipt.log_dir or Path(get_hermes_home()) / "logs" / "reply-latency"
        _ensure_log_directory(directory)
        day = datetime.fromtimestamp(timestamp, UTC).date().isoformat()
        # A single O_APPEND write keeps concurrent turns/processes from interleaving JSON lines.
        fd = os.open(directory / f"{day}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (payload + "\n").encode("utf-8"))
        finally:
            os.close(fd)
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
        from hermes_constants import get_hermes_home
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
        }, log_dir=Path(get_hermes_home()) / "logs" / "reply-latency")
        receipt.identity["inbound_key"] = [receipt.identity[name] for name in
                                           ("platform", "chat_id", "thread_id", "message_id")]
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


def record_admitted(event: Any) -> None:
    """The durable FIFO accepted this inbound, before it waits for execution."""
    receipt = record_received(event)
    if receipt is not None and not receipt.admitted:
        receipt.admitted = True
        _emit(receipt, "admitted", time.time())


def current_receipt() -> ReplyReceipt | None:
    return _current.get()


def restore_receipt(receipt: ReplyReceipt | None) -> None:
    """Restore a recursive turn's parent without emitting another agent_start."""
    _current.set(receipt)


@contextmanager
def delivery_scope(receipt: ReplyReceipt | None, kind: str):
    """Carry a turn's receipt into adapter chunk confirmations, including scheduled progress."""
    token, kind_token = _current.set(receipt), _delivery_kind.set(kind)
    try:
        yield
    finally:
        _delivery_kind.reset(kind_token)
        _current.reset(token)


def confirmed(result: Any) -> bool:
    raw = getattr(result, "raw_response", None)
    return (getattr(result, "success", False) is True
            and getattr(result, "deferred", False) is not True
            and not (isinstance(raw, dict) and raw.get("skipped")))


def record_first_reply(
    receipt: ReplyReceipt | None, text: str, result: Any = None, *, kind: str = "text_send",
) -> float | None:
    """Call only at a confirmed transport success, never a journal/optimistic mark."""
    if receipt is None or not text.strip() or (result is not None and not confirmed(result)):
        return
    raw = getattr(result, "raw_response", None)
    stamp = raw.get("reply_latency_confirmed_at", time.time()) if isinstance(raw, dict) else time.time()
    receipt.last_delivery_at, receipt.last_delivery_kind = stamp, kind
    receipt.last_delivery_text = text.strip()
    if not receipt.replied:
        receipt.replied = True
        _emit(receipt, "first_reply", stamp, delivery_kind=kind)
    return stamp


def record_transport_reply(text: str, *, chat_id: str, thread_id: Any = None) -> float | None:
    """Only adapters call this, immediately after an actual API confirmation."""
    receipt, kind = current_receipt(), _delivery_kind.get()
    thread = str(thread_id) if thread_id is not None else None
    if (receipt is None or kind is None or str(chat_id) != receipt.identity["chat_id"]
            or thread != receipt.identity["thread_id"]):
        return None
    return record_first_reply(receipt, text, kind=kind)


def transport_confirmed(text: str, result: Any, *, chat_id: str, thread_id: Any = None) -> Any:
    """Attach the API clock before adapter bookkeeping or an outbox commit can delay return."""
    stamp = record_transport_reply(text, chat_id=chat_id, thread_id=thread_id)
    if stamp is not None:
        result.raw_response = {**(result.raw_response or {}), "reply_latency_confirmed_at": stamp}
    return result


def record_final_reply(receipt: ReplyReceipt | None, text: str, result: Any, *, kind="text_send") -> None:
    stamp = record_first_reply(receipt, text, result, kind=kind)
    if stamp is not None:
        _emit(receipt, "final_delivery", stamp, delivery_kind=kind)


def record_stream_final(receipt: ReplyReceipt | None, text: str) -> None:
    """Seal with the last real transport clock, never the optimistic delivered flag's clock."""
    if (receipt is not None and receipt.last_delivery_at is not None
            and (receipt.last_delivery_kind or "").startswith("text_")
            and text.strip() and receipt.last_delivery_text == text.strip()):
        _emit(receipt, "final_delivery", receipt.last_delivery_at,
              delivery_kind=receipt.last_delivery_kind)
