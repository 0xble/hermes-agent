"""Event-driven replacement of one delegation presentation, never a heartbeat."""
from __future__ import annotations

import asyncio
import time

from gateway import delegation_card_presentation as presentation

# Six distinct ordinary messages in this topic; no elapsed-time eligibility gate.
# Transport spacing and server flood cooldowns remain owned by the shared gate.
# IDs are deduplication tokens, never a measure of topic displacement.
DISPLACEMENT = 6


def observe_conversation(manager, adapter, chat_id, thread_id, message_id):
    if not isinstance(message_id, (str, int)) or isinstance(message_id, bool):
        return
    identity = (id(adapter), str(chat_id), str(thread_id or ""), str(message_id))
    if identity in manager.observed_messages:
        return
    manager.observed_messages[identity] = None
    if len(manager.observed_messages) > 1024:
        manager.observed_messages.pop(next(iter(manager.observed_messages)))
    for key, card in manager.cards.items():
        if manager._anchor(key) != key or not card.get("message_id"):
            continue
        source = manager._source(card)
        if (source is None or str(source.chat_id) != str(chat_id)
                or str(source.thread_id or "") != str(thread_id or "")
                or manager.runner._adapter_for_source(source) is not adapter):
            continue
        if str(message_id) == card["message_id"]:
            continue
        seen = manager.displacement.setdefault(key, set())
        if len(seen) < DISPLACEMENT:
            seen.add(str(message_id))
        if eligible(manager, key):
            manager._queue(key)


def eligible(manager, key):
    card = manager.cards[key]
    return (bool(card.get("message_id")) and not card.get("reanchor")
            and not any(c.get("obsolete_message_id") for _, c in manager._members(key))
            and not presentation.pending(manager, key) and not presentation.fenced(manager, key)
            and len(manager.displacement.get(key, ())) >= DISPLACEMENT
            and any(r.get("state") == "running" for r in manager._projection(key)["rows"].values()))


def pending(card):
    return (card.get("reanchor") or {}).get("order") == "delete_first"


def adopt_receipt(manager, card):
    receipt = card.get("reanchor") or {}
    if (receipt.get("state") != "sent" or card.get("message_deleted")
            or card.get("consolidated_message_id")
            or card.get("message_id") == receipt.get("new_message_id")):
        return
    # Legacy send-first receipts still need exact-old cleanup. Never reinterpret
    # their attempting state as proof that either transport is absent.
    if receipt.get("order") != "delete_first":
        card["obsolete_message_id"] = receipt["old_message_id"]
    card["message_id"] = receipt["new_message_id"]
    card["rendered"] = receipt["rendered"]
    card["anchored_at"] = receipt["sent_at"]
    card["delete_attempts"] = 0
    card.pop("message_deleted", None)
    if pending(card):
        card["last_reanchor"] = card.pop("reanchor")
    manager._save()


async def replace(manager, key):
    """Delete confirmed absent → fresh send; a gap is preferable to two cards.

    Do not hold the lifecycle lock over transport waits: completion/handling must
    remain observable while the shared outbound gate gives final replies priority.
    The manager owns one flush per anchor, and bind preserves an in-flight anchor.
    """
    from gateway.delegation_cards import render_card

    card = manager.cards[key]
    lock = manager.locks.setdefault(manager._scope(card), asyncio.Lock())
    async with lock:
        source = manager._source(card)
        adapter = manager._adapter(card, source)
        if adapter is None:
            return
        if not pending(card):
            if not eligible(manager, key):
                return
            card["reanchor"] = dict(order="delete_first", state="delete_pending",
                                    old_message_id=card["message_id"], delete_attempts=0, send_attempts=0)
            manager._save()
        receipt = card["reanchor"]
        if receipt["state"] in {"delete_pending", "deleting"}:
            if receipt["delete_attempts"] >= 3 or manager._defer_delete(card, adapter):
                return
            receipt["state"] = "deleting"
            receipt["delete_attempts"] += 1
            manager._save()  # write-ahead, including cancellation/crash uncertainty
        elif receipt["state"] != "deleted":
            return  # sending is ambiguous, including after restart: never resend

    if receipt["state"] == "deleting":
        status_delete = getattr(type(adapter), "_delete_status_message", None)
        if status_delete is not None:
            deleted = await status_delete(adapter, source.chat_id, receipt["old_message_id"])
        else:
            deleted = await adapter.delete_message(source.chat_id, receipt["old_message_id"])
        async with lock:
            if deleted is not True:
                if deleted is None and status_delete is not None:
                    receipt["delete_attempts"] -= 1
                    receipt["state"] = "delete_pending"
                    manager._defer_delete(card, adapter, minimum_delay=1.0)
                elif receipt["delete_attempts"] < 3:
                    manager._defer_delete(card, adapter)
                manager._save()
                return
            receipt["state"] = "deleted"
            card.update(message_id=None, message_deleted=True, rendered="", send_attempts=1)
            manager.displacement.pop(key, None)
            manager._save()

    async with lock:
        if receipt["send_attempts"] >= 3:
            return
        projection = manager._projection(key)
        if not any(r.get("state") == "running" for r in projection["rows"].values()):
            return  # terminal-only/handled during the gap must not resurrect a card
        receipt["state"] = "sending"
        receipt["send_attempts"] += 1
        manager._save()

    def latest():
        # Called synchronously after the adapter's scheduler awaits, immediately
        # before the Bot API. No lifecycle mutation can interleave with this check.
        projection = manager._projection(key)
        if not any(r.get("state") == "running" for r in projection["rows"].values()):
            return None
        receipt["rendered"] = render_card(projection)
        manager._save()
        return receipt["rendered"]

    result = await adapter.send_delegation_card(source, latest)
    async with lock:
        if getattr(result, "success", False) and getattr(result, "message_id", None):
            receipt.update(state="sent", new_message_id=str(result.message_id), sent_at=time.time())
            card.pop("message_deleted", None)
            manager._save()
            adopt_receipt(manager, card)
            # A terminal/handled event may have arrived during the actual request.
            # Reconcile that fresh state rather than overwrite it with sent text.
            manager.cards[key]["revision"] = card.get("revision", 0) + 1
        else:
            raw = getattr(result, "raw_response", None) or {}
            retry_after = getattr(result, "retry_after", None)
            if raw.get("cancelled_before_send"):
                receipt["state"] = "deleted"
                receipt["send_attempts"] -= 1
            elif raw.get("definite_rejection") or (getattr(result, "retryable", False) and retry_after is not None):
                receipt["state"] = "deleted"
                if receipt["send_attempts"] < 3:
                    card["retry_at"] = time.time() + max(manager.interval, 1.0, float(retry_after or 0))
            # Every other failure retains sending: a lost receipt fences resends.
            manager._save()
