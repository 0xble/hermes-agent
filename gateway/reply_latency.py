"""Local receipt-to-delivery records, independent of opt-in shared telemetry.

Keep correlation on the event and its stream consumer, not the chat: multiple
threads and queued turns can be in flight on the same adapter at once.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
import threading
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gateway.attended_model import is_genuine_user_message
from gateway.platforms.event import turn_origin_for_event

logger = logging.getLogger(__name__)


@dataclass
class ReplyReceipt:
    identity: dict[str, Any]
    home: Path
    private: bool = False
    started: bool = False
    replied: bool = False
    no_reply: bool = False
    absorbed: tuple[ReplyReceipt, ...] = ()
    lock: Any = field(default_factory=threading.Lock, repr=False)


_current: contextvars.ContextVar[ReplyReceipt | None] = contextvars.ContextVar(
    "gateway_reply_receipt", default=None)


# Share live event copies without keeping completed receipts alive forever.
_live: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


def _emit(receipt: ReplyReceipt, event: str, timestamp: float) -> None:
    from agent.incognito import is_incognito
    # Decide on the calling context, before crossing to a context-free disk worker.
    # A private receipt stays private even when its delivery outlives that scope.
    if receipt.private or is_incognito():
        receipt.private = True
        return
    payload = {**receipt.identity, "event": event, "timestamp": timestamp}
    try:
        from gateway.reply_latency_sink import append_event
        append_event(receipt.home, payload)
    except Exception:
        # Path/diagnostic setup failures are as non-fatal as append failures.
        logger.debug("Could not access reply latency sink", exc_info=True)
    try:
        logger.info("gateway_reply_latency %s", json.dumps(
            payload,
            separators=(",", ":"), sort_keys=True))
    except Exception:
        logger.debug("Could not emit reply latency record", exc_info=True)


def stamp_received(event: Any) -> float | None:
    """Preserve the arrival clock before admission, without logging unauthorized input."""
    if not is_genuine_user_message(event):
        return None
    if not getattr(event, "_reply_latency_receipt_id", None):
        event._reply_latency_receipt_id = uuid.uuid4().hex
    stamp = getattr(event, "_reply_latency_received_at", None)
    if stamp is None:
        stamp = getattr(event, "_attended_activity_received_at", None)
    if stamp is None:
        stamp = time.time()
        event._attended_activity_received_at = stamp
    event._reply_latency_received_at = stamp
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
        from gateway.session_identity import proven_runtime_home
        from hermes_constants import get_hermes_home

        source = getattr(event, "source", None)
        if source is None:
            return None
        message_id = getattr(event, "message_id", None)
        thread_id = getattr(source, "thread_id", None)
        profile = str(getattr(source, "profile", None) or current_profile_name("default"))
        platform = str(getattr(source.platform, "value", source.platform))
        inbound = [profile, platform, str(source.chat_id),
                   str(thread_id) if thread_id is not None else None,
                   str(message_id) if message_id is not None else None]
        home = proven_runtime_home(source) or get_hermes_home()
        key = (str(home), tuple(inbound) if message_id is not None else event._reply_latency_receipt_id)
        if existing := _live.get(key):
            from agent.incognito import is_incognito
            existing.private = existing.private or is_incognito()
            event._reply_latency_receipt = existing
            return existing
        from agent.incognito import is_incognito
        receipt = ReplyReceipt({
            "inbound_identity": inbound,
            "receipt_id": event._reply_latency_receipt_id,
            # Reuse transport identity already known at receipt. An outbox admission/active
            # token assigned later cannot identify the original queue wait without a new ID.
            "turn_id": transport_id(event),
            "message_id": str(message_id) if message_id is not None else None,
            "platform": platform,
            "origin": turn_origin_for_event(event),
            "profile": profile,
            "chat_id": str(source.chat_id),
            "thread_id": str(thread_id) if thread_id is not None else None,
        }, home=home, private=is_incognito())
        _live[key] = receipt
        event._reply_latency_receipt = receipt
        _emit(receipt, "received", stamp)
        return receipt
    except Exception:
        logger.debug("Could not build reply latency receipt", exc_info=True)
        return None


def snapshot_inputs(event: Any) -> list[dict[str, Any]]:
    """Content-free merged receipt provenance, independent of attended's newest clock."""
    inputs = []
    seen = set()
    for candidate in (event, *getattr(event, '_hermes_absorbed_events', ())):
        prior = getattr(candidate, '_reply_latency_inputs', None)
        rows = prior or [{'message_id': getattr(candidate, 'message_id', None),
                          'received_at': getattr(candidate, '_reply_latency_received_at',
                                                 getattr(candidate, '_attended_activity_received_at', None)),
                          'receipt_id': getattr(candidate, '_reply_latency_receipt_id', None)
                                        if getattr(candidate, 'message_id', None) is None else None}]
        for row in rows:
            key = (row['message_id'], row.get('receipt_id'))
            if key not in seen:
                inputs.append(dict(row))
                seen.add(key)
    return inputs


def restore_inputs(event: Any, inputs: list[dict[str, Any]]) -> None:
    import math
    if (not isinstance(inputs, list) or not inputs
            or any(not isinstance(item, dict) or 'message_id' not in item
                   or not isinstance(item.get('received_at'), (int, float))
                   or isinstance(item.get('received_at'), bool)
                   or not math.isfinite(item['received_at']) for item in inputs)):
        return
    # Copy descriptors so legacy id-less rows get one stable token across rebinds.
    rows = [dict(item) for item in inputs]
    for item in rows:
        if item['message_id'] is None and not item.get('receipt_id'):
            item['receipt_id'] = uuid.uuid4().hex
    event._reply_latency_inputs = rows
    event._reply_latency_received_at = rows[0]['received_at']
    if rows[0].get('receipt_id'):
        event._reply_latency_receipt_id = rows[0]['receipt_id']


def _latency_input_events(event: Any):
    """Rebuild content-free receipt carriers without granting input provenance."""
    from types import SimpleNamespace
    for candidate in (event, *getattr(event, '_hermes_absorbed_events', ())):
        yield candidate
        for item in (getattr(candidate, '_reply_latency_inputs', None) or [])[1:]:
            yield SimpleNamespace(
                source=candidate.source, message_id=item['message_id'], internal=False,
                metadata={}, _reply_latency_received_at=item['received_at'],
                _attended_activity_received_at=item['received_at'],
                _reply_latency_receipt_id=item.get('receipt_id') or uuid.uuid4().hex,
            )


def bind_agent_start(event: Any) -> None:
    """Bind the admitted event so newly constructed consumers retain its own receipt.

    Event-less interrupt text has no attributable inbound message. Clear the parent
    receipt rather than logging this text as an answer to the chain head.
    """
    receipt = record_received(event)
    if receipt is not None:
        # Private restored carriers never enter _hermes_absorbed_events: that trail
        # is authoritative human/synthetic provenance, not latency instrumentation.
        receipts = [r for candidate in _latency_input_events(event)
                    if (r := record_received(candidate)) is not None and r is not receipt]
        receipt.absorbed = tuple({id(r): r for r in receipts}.values())
    _current.set(receipt)  # synthetic turns must clear an inherited human clock
    if receipt is not None:
        for item in (receipt, *receipt.absorbed):
            with item.lock:
                if not item.started:
                    item.started = True
                    _emit(item, "agent_start", time.time())


def current_receipt() -> ReplyReceipt | None:
    return _current.get()


def restore_receipt(receipt: ReplyReceipt | None) -> None:
    """Restore a recursive turn's parent without emitting another agent_start."""
    _current.set(receipt)


def record_first_reply(receipt: ReplyReceipt | None, text: str, result: Any = None) -> None:
    """Call only at a confirmed transport success, never a journal/optimistic mark."""
    if receipt is None or not text.strip():
        return
    if result is not None:
        raw = getattr(result, "raw_response", None)
        if getattr(result, "success", False) is not True or (isinstance(raw, dict) and raw.get("skipped")):
            return
    _record_terminal(receipt, "first_reply")


def _record_terminal(receipt: ReplyReceipt | None, event: str) -> None:
    if receipt is None:
        return
    stamp = time.time()
    for item in (receipt, *receipt.absorbed):
        with item.lock:
            if item.replied or item.no_reply:
                continue
            if event == "first_reply":
                item.replied = True
            else:
                item.no_reply = True
            _emit(item, event, stamp)


def record_no_reply(receipt: ReplyReceipt | None) -> None:
    """A deliberate silent ending settles the input without inventing a delivery."""
    _record_terminal(receipt, "no_reply")


def event_receipt(event: Any) -> ReplyReceipt | None:
    return getattr(event, "_terminal_reply_latency_receipt",
                   getattr(event, "_reply_latency_receipt", None))


def hand_over_receipt(source: Any, target: Any) -> None:
    """Execution and transport waiters own different events, but one delivery clock."""
    for name in ("_reply_latency_receipt", "_terminal_reply_latency_receipt"):
        if hasattr(source, name):
            setattr(target, name, getattr(source, name))


def _thread_identity(thread_id: Any) -> str | None:
    """Session routing uses '' for no thread; retain nonempty IDs, including zero."""
    return (str(thread_id) or None) if thread_id is not None else None


def record_tool_reply(platform: str, chat_id: Any, thread_id: Any = None) -> None:
    """Only a successful delivery to this turn's destination answers its input."""
    receipt = current_receipt()
    if receipt is None:
        return
    identity = receipt.identity
    if (identity['platform'] == platform and identity['chat_id'] == str(chat_id)
            and _thread_identity(identity['thread_id']) == _thread_identity(thread_id)):
        record_first_reply(receipt, "delivery")
