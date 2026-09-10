"""Event-driven replacement of one delegation presentation, never a heartbeat."""
from __future__ import annotations

import time

from gateway import delegation_card_presentation as presentation

# Eight distinct ordinary messages in this topic, and at most one move per five
# minutes. IDs are deduplication tokens, never a measure of topic displacement.
DISPLACEMENT = 8
COOLDOWN = 300.0


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
            and time.time() - max(card.get("anchored_at", 0), manager.tracking_started) >= COOLDOWN
            and any(r.get("state") == "running" for r in manager._projection(key)["rows"].values()))


def adopt_receipt(manager, card):
    receipt = card.get("reanchor") or {}
    if (receipt.get("state") != "sent" or card.get("message_deleted")
            or card.get("consolidated_message_id")
            or card.get("message_id") == receipt.get("new_message_id")):
        return
    # The new transport receipt was persisted before changing the active anchor.
    card["obsolete_message_id"] = receipt["old_message_id"]
    card["message_id"] = receipt["new_message_id"]
    card["rendered"] = receipt["rendered"]
    card["anchored_at"] = receipt["sent_at"]
    card["delete_attempts"] = 0
    card.pop("message_deleted", None)
    manager._save()


async def replace(manager, key, adapter, source, text):
    card = manager.cards[key]
    card["reanchor"] = {"state": "attempting", "old_message_id": card["message_id"]}
    manager._save()  # An ambiguous send/crash must fence every subsequent replacement.
    result = await adapter.send_delegation_card(source, text)
    if getattr(result, "success", False) and getattr(result, "message_id", None):
        card["reanchor"].update(state="sent", new_message_id=str(result.message_id),
                                rendered=text, sent_at=time.time())
        manager._save()
        adopt_receipt(manager, card)
        manager.displacement.pop(key, None)
    elif getattr(result, "retryable", False) and getattr(result, "retry_after", None) is not None:
        card.pop("reanchor", None)  # Explicit scheduler/429 rejection, not ambiguity.
        card["retry_at"] = time.time() + max(0.05, float(result.retry_after))
        manager._save()
    elif (getattr(result, "raw_response", None) or {}).get("definite_rejection"):
        card.pop("reanchor", None)
        manager.displacement.pop(key, None)  # A new displacement window may try again.
        card["anchored_at"] = time.time()
        manager._save()
    return result
