"""Distinguish user edits from Slack's thread and rendering metadata updates."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .adapter import SlackAdapter

logger = logging.getLogger(__name__)


def log_message_ingress(event: dict, payload: dict | None) -> None:
    """Keep an ingress receipt before filtering, without recording message content."""
    message = event.get("message")
    nested = message if isinstance(message, dict) else {}
    logger.info(
        "[Slack] ingress: event_id=%s type=%s subtype=%s user=%s channel=%s "
        "ts=%s event_ts=%s message_ts=%s thread_ts=%s",
        (payload or {}).get("event_id", ""), event.get("type", ""), event.get("subtype", ""),
        event.get("user") or nested.get("user", ""), event.get("channel", ""),
        event.get("ts", ""), event.get("event_ts", ""), nested.get("ts", ""),
        event.get("thread_ts") or nested.get("thread_ts", ""),
    )


def _file_ids(message: dict) -> set[str]:
    return {
        str(file["id"])
        for file in (message.get("files") or [])
        if isinstance(file, dict) and file.get("id")
    }


def _is_user_edit(event: dict, updated: dict, edited_ts: str) -> bool:
    previous = event.get("previous_message")
    if isinstance(previous, dict):
        # An old edited marker survives later status, unfurl and reply-count updates.
        # Compare authored content, not generated blocks or mutable file metadata.
        return (
            (previous.get("text") or "") != (updated.get("text") or "")
            or _file_ids(previous) != _file_ids(updated)
        )
    if not edited_ts:
        return False
    # Slack documents the envelope ts as the edit ts. Without a previous snapshot,
    # an older retained marker is not evidence that this event is a user edit.
    change_times = {
        str(event.get(key) or "") for key in ("event_ts", "ts")
    } - {"", str(updated.get("ts") or "")}
    return not change_times or edited_ts in change_times


def normalize_changed_message(adapter: SlackAdapter, event: dict) -> dict | None:
    """Route actual edits of unseen messages, never metadata-only root updates."""
    updated = event.get("message")
    if not isinstance(updated, dict):
        return None
    original_ts = str(updated.get("ts") or "")
    if original_ts and original_ts in adapter._processed_message_ts:
        return None
    edited = updated.get("edited")
    edited_ts = str(edited.get("ts") or "") if isinstance(edited, dict) else ""
    if not _is_user_edit(event, updated, edited_ts):
        logger.info(
            "[Slack] Ignoring metadata-only message_changed: channel=%s message_ts=%s "
            "event_ts=%s previous=%s edited_ts=%s",
            event.get("channel", ""), original_ts, event.get("event_ts") or event.get("ts") or "",
            isinstance(event.get("previous_message"), dict), edited_ts,
        )
        return None
    outer_ts = str(event.get("ts") or "")
    changed_ts = (
        str(event.get("event_ts") or edited_ts or "")
        or (outer_ts if outer_ts != original_ts else "")
        or (f"{original_ts}:changed" if original_ts else "")
    )
    normalized = dict(updated)
    for key in ("channel", "channel_type", "team", "team_id"):
        if not normalized.get(key) and event.get(key):
            normalized[key] = event[key]
    if changed_ts:
        normalized["_slack_changed_event_ts"] = changed_ts
    return normalized
