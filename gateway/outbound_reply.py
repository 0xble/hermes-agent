"""Exact assistant-row identity for a delivered final.

The assistant-row UID stays on the gateway event (or an explicit queued-lane
argument) until the ledgered send stamps it. Adapter metadata remains public
routing state only.
"""

from __future__ import annotations

from typing import Any


def stamp_gateway_assistant_entry(entry: dict, event: Any) -> None:
    """Bind the gateway event to the assistant row it is writing.

    Persistence walks a gateway-written or proxy turn forward. A tool-call
    assistant row is not the visible final, so each assistant entry replaces
    the event UID, including one already stamped by an earlier row.
    """
    if entry.get("role") != "assistant":
        return
    uid = entry.get("message_uid")
    if not (isinstance(uid, str) and uid):
        from agent.message_metadata import stamp_message_uid
        uid = stamp_message_uid(entry)
    event._transcript_message_uid = uid


def ensure_reply_uid(result: Any) -> str | None:
    """Ensure the assistant row that gateway persistence will write has a UID."""
    if not isinstance(result, dict):
        return None
    messages = result.get("messages")
    if isinstance(messages, list):
        for msg in reversed(messages):
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            uid = msg.get("message_uid")
            if isinstance(uid, str) and uid:
                return uid
            from agent.message_metadata import stamp_message_uid
            return stamp_message_uid(msg)
    if not result.get("final_response"):
        return None
    uid = result.get("_queued_reply_uid")
    if not (isinstance(uid, str) and uid):
        from agent.message_metadata import stamp_message_uid
        uid = stamp_message_uid({"role": "assistant", "content": result["final_response"]})
        result["_queued_reply_uid"] = uid
    return uid


def reply_uid_for_final(event: Any, metadata: dict | None) -> str | None:
    """The assistant UID a final send should stamp, or None for a copy block."""
    if (metadata or {}).get("copy_block"):
        return None
    uid = getattr(event, "_transcript_message_uid", None)
    return str(uid) if isinstance(uid, str) and uid else None
