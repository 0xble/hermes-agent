"""Topical helpers extracted from agent/conversation_loop.py."""
from __future__ import annotations
from typing import Any
import logging

logger = logging.getLogger("agent.conversation_loop")

def _close_durable_failed_turn(agent, result: Any) -> None:
    """Append a Hermes-authored assistant boundary when a failed turn left its durable tail open
    (``OPEN_TURN_TAIL_ROLES``: the accepted ``user`` row, or a ``tool`` row when a call ran before
    the failure), in place on ``result["messages"]`` and in SessionDB.

    The terminal-failure paths (content-policy refusal, ``_Trunc.end_turn``, retry exhaustion,
    interrupt before any assistant text) persist the accepted user row and return without
    reaching ``finalize_turn``; the next prompt then appends a second user row and
    ``repair_message_sequence`` merges the failed request into the new one. The gateway
    compensates with ``_hmwa_close_failed_turn``; CLI, TUI/Desktop and ACP hosts hand
    ``result["messages"]`` straight back as history, so the seam is here.

    Excluded: the context-pressure classes (``compression_exhausted``, ``compression_deferred``,
    ``failure_reason == "context_overflow"``) — appending to an already-oversized session is the
    #1630 growth loop; their repair is rotation or a retry. ``content_policy_blocked`` too: the
    WHOLE refused turn is erased from the conversation — the user message, any tool rounds that ran,
    and the refusal (the owner rewinds it durably: the gateway owner-side for Bot Chat, the CLI/ACP
    owner for their direct DB; in-memory surfaces return the pre-refusal history). Erasing the whole
    turn (tool rounds included) is deliberate: the user keeps SEEING the message in the chat, but the
    model is never re-sent the content that tripped the filter, so the chat is not locked. A closing
    boundary would only be written to be rewound. Idempotence
    is keyed on the DURABLE tail (``SessionDB.latest_conversation_role``), so a redelivery or a tail
    already closed by another writer is a no-op, and the gateway's own closer then no-ops in turn.
    """
    from agent.conversation_loop import (FAILED_TURN_DISPLAY_KIND, OPEN_TURN_TAIL_ROLES, _failed_turn_display_metadata, append_message, failed_turn_notice)
    try:
        if not isinstance(result, dict) or result.get("completed") is True:
            return
        if (
            result.get("compression_exhausted") or result.get("compression_deferred")
            or result.get("failure_reason") in ("context_overflow", "content_policy_blocked")
        ):
            return
        messages = result.get("messages")
        db, session_id = getattr(agent, "_session_db", None), getattr(agent, "session_id", None)
        if not isinstance(messages, list) or not messages or db is None or not session_id:
            return
        if getattr(agent, "_persist_disabled", False) or db.latest_conversation_role(session_id) not in OPEN_TURN_TAIL_ROLES:
            return
        # Scope the "did a tool run" scan to this turn when its boundary is proven; otherwise
        # hedge over the whole list rather than under-report a possible side effect.
        start = result.get("current_turn_user_idx")
        turn_messages = messages[start:] if isinstance(start, int) and 0 <= start < len(messages) else messages
        boundary = {
            "role": "assistant", "content": failed_turn_notice(turn_messages), "display_kind": FAILED_TURN_DISPLAY_KIND,
        }
        if failure := _failed_turn_display_metadata(agent, result):
            boundary["display_metadata"] = failure
        append_message(messages, boundary)
        agent._flush_messages_to_session_db(messages)
    except Exception:
        logger.debug("failed-turn boundary not written", exc_info=True)
