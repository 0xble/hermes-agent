"""Exact assistant-row identity for a delivered final.

Queued-lane metadata is built before persistence chooses the assistant row.
The normal final refreshes from the event after that choice; this module is
the same refresh for a send that still holds the earlier metadata.
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


def assistant_reply_uid(messages: Any) -> str | None:
    """The last assistant UID in a result message list."""
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            uid = msg.get("message_uid")
            if isinstance(uid, str) and uid:
                return uid
    return None


def queued_reply_metadata(metadata: dict | None, result: Any) -> dict | None:
    """Queued-lane metadata refreshed after the assistant row is knowable.

    The lane sends with turn metadata built before persistence copies the uid
    onto the event. Bind the uid to the result first so proxy and fallback rows
    use the same identity when the gateway writes them.
    """
    uid = ensure_reply_uid(result)
    if not uid:
        return refresh_reply_metadata(metadata, messages=result.get("messages") if isinstance(result, dict) else None)
    return refresh_reply_metadata(
        {**(metadata or {}), "_transcript_message_uid": uid},
        messages=result.get("messages") if isinstance(result, dict) else None,
    )


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


def refresh_reply_metadata(metadata: dict | None, event: Any = None, messages: Any = None) -> dict | None:
    """Metadata for a final send, including the transcript uid when one is known.

    Copy blocks stay unstamped. An event uid (persistence already chose the row)
    wins over a uid already on metadata; otherwise the last assistant uid on the
    turn's messages is used. Metadata built before that choice is what the
    queued lane holds.
    """
    if (metadata or {}).get("copy_block"):
        return metadata
    uid = getattr(event, "_transcript_message_uid", None) if event is not None else None
    if not (isinstance(uid, str) and uid):
        uid = (metadata or {}).get("_transcript_message_uid")
    if not (isinstance(uid, str) and uid):
        uid = assistant_reply_uid(messages)
    if not (isinstance(uid, str) and uid) or (metadata or {}).get("_transcript_message_uid") == uid:
        return metadata
    return {**(metadata or {}), "_transcript_message_uid": str(uid)}


def reply_uid_for_final(event: Any, metadata: dict | None) -> str | None:
    """The assistant uid a final send should stamp, or None for a copy block."""
    refreshed = refresh_reply_metadata(metadata, event=event)
    if (refreshed or {}).get("copy_block"):
        return None
    uid = (refreshed or {}).get("_transcript_message_uid")
    return str(uid) if isinstance(uid, str) and uid else None


def adapter_send_metadata(metadata: dict | None) -> dict | None:
    """Public adapter metadata, without the gateway-internal transcript UID.

    The UID names the assistant row the stamp writes after a successful send.
    Adapters and public metadata assertions must not see it. None stays None
    so callers that distinguish absent metadata from an empty dict stay exact.
    """
    if not isinstance(metadata, dict) or "_transcript_message_uid" not in metadata:
        return metadata
    public = {key: value for key, value in metadata.items() if key != "_transcript_message_uid"}
    return public or None
