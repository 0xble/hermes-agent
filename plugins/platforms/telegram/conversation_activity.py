"""Ordinary-message observations for the gateway's delegation presentation owner."""
import logging

logger = logging.getLogger(__name__)


def record(adapter, chat_id, thread_id, message_id):
    observer = vars(adapter).get("_delegation_conversation_observer")
    if observer is not None:
        try:
            observer(chat_id, thread_id, message_id)
        except Exception:
            # Presentation bookkeeping must never invalidate a delivered reply.
            logger.exception("Delegation conversation observation failed")


def inbound(adapter, update):
    # New ordinary messages only: edits/reactions/drafts do not displace a card.
    message = getattr(update, "message", None)
    if message is not None:
        record(adapter, getattr(message, "chat_id", None),
               getattr(message, "message_thread_id", None), getattr(message, "message_id", None))


def physical_outbound(adapter, chat_id, message, metadata, thread_id=None):
    """One acknowledged new message, never an edit/draft/card/status update."""
    if (metadata or {}).get("hermes_status"):
        return
    messages = message if isinstance(message, (tuple, list)) else [message]
    for sent in messages:
        record(adapter, chat_id, getattr(sent, "message_thread_id", thread_id),
               getattr(sent, "message_id", None))


def outbound(adapter, chat_id, result, metadata):
    if not result.success or (metadata or {}).get("hermes_status"):
        return
    raw = result.raw_response or {}
    # A topic-fallback receipt must not count toward the originally requested topic.
    thread = None if raw.get("thread_fallback") else adapter._metadata_thread_id(metadata)
    for message_id in raw.get("message_ids") or [result.message_id]:
        record(adapter, chat_id, thread, message_id)
