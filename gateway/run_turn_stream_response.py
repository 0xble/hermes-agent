"""Delivery of a response whose visible body was already streamed."""

from __future__ import annotations

import logging
from contextlib import suppress

logger = logging.getLogger(__name__)

async def deliver_streamed_turn_response(
    runner, event, source, session_entry, session_key, run_generation,
    agent_result, agent_messages, response, footer_line, adapter,
    is_ephemeral_response, ephemeral_ttl, copy_blocks, response_without_copy,
):
    """Deliver deferred media, copy blocks, restart notices, and footer for a streamed turn."""
    from gateway.run_turn_incognito_notice import apply_streamed_incognito_restart_notice

    media_delivered = False
    copy_failed = False
    copy_delivered = False
    if (adapter and copy_blocks and not agent_result.get("interrupted")
            and not agent_result.get("copy_already_delivered")):
        copy_results = []
        await adapter._send_copy_blocks(
            event, session_key, copy_blocks, runner._event_thread_metadata(event, source) or {},
            copy_results.append, is_ephemeral_response=is_ephemeral_response,
            ephemeral_ttl=ephemeral_ttl,
        )
        copy_delivered = any(getattr(result, "success", False) for result in copy_results)
        copy_failed = any(not getattr(result, "success", False) for result in copy_results)
    # Interrupted copy fragments already appeared inline in the stream; never append a second
    # unquoted copy of those partial bodies.
    if copy_failed:
        agent_result["failed"] = True
        agent_result["error"] = "one or more [[copy]] blocks failed to deliver"
    if response_without_copy and adapter and not agent_result.get("media_already_delivered"):
        from gateway.copy_blocks import strip_copy_blocks
        media_delivered = bool(await runner._deliver_media_from_response(
            strip_copy_blocks(response), event, adapter))
    response = await apply_streamed_incognito_restart_notice(
        runner, session_entry, response, adapter, source, event)
    # Streaming delivered the body, but the footer was held back (`not already_sent` gate).
    if footer_line and adapter:
        try:
            await adapter.send(source.chat_id, footer_line, metadata=runner._event_thread_metadata(event, source))
        except Exception as exc:  # health: allow BLE001 -- footer delivery must not fail an already-streamed turn; traceback is logged
            logger.debug("trailing footer send failed: %s", exc, exc_info=True)
    # Reconcile the restart note only on a confirmed answer: streamed visible text, or a
    # confirmed upload for an attachment-only answer. A failed upload keeps the note so a
    # later delivery can still reconcile it (the queued lane reconciles its own uploads).
    if adapter and hasattr(adapter, "_reconcile_restart_note_after_delivery"):
        streamed_text = False
        if response_without_copy:
            try:
                from gateway.run import _strip_response_attachments_for_direct_send
                streamed_text = bool(_strip_response_attachments_for_direct_send(response, adapter))
            except Exception as exc:  # health: allow BLE001 -- fail open so a delivered stream still reconciles; traceback is logged
                logger.debug("streamed attachment strip failed open: %s", exc, exc_info=True)
                streamed_text = True  # fail open to the historical unconditional reconcile
        if streamed_text or media_delivered or (copy_delivered and not copy_failed):
            await adapter._reconcile_restart_note_after_delivery(event, session_key)
    # Return None so the body isn't sent twice; stash the raw text on the event for the
    # /loop and /goal hooks that read the return value.
    with suppress(Exception):
        from gateway.stream_recovery import unmark_recovered_reply
        event._streamed_final_response = unmark_recovered_reply(
            str(response or ""), agent_result)
    delivered_id = agent_result.get("_delivered_platform_message_id")
    reply_uid = getattr(event, "_transcript_message_uid", None)
    if delivered_id and str(delivered_id) != "__no_edit__" and reply_uid:
        from gateway.platforms.base import _schedule_outbound_platform_stamp
        _schedule_outbound_platform_stamp(
            getattr(runner, "session_store", None), session_key,
            str(delivered_id), str(reply_uid))
    return None
