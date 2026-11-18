"""Recover completed replies from deleted Telegram topics at their final-delivery boundary."""

import asyncio
import logging

from gateway.config import Platform

logger = logging.getLogger("gateway.platforms.base")


async def recover_deleted_topic_final(adapter, chat_id, content, metadata, result, error_str):
    # A completed reply to a deleted private Telegram topic cannot be repaired by
    # formatting fallback: that keeps both the dead thread and its reply anchor.
    # Never move interim output (or an ambiguous timeout) into the parent DM.
    topic_meta = metadata if isinstance(metadata, dict) else {}
    if (
        adapter.platform == Platform.TELEGRAM
        and topic_meta.get("notify") and not topic_meta.get("_interim_send")
        and topic_meta.get("telegram_dm_topic_reply_fallback")
        and not topic_meta.get("telegram_stale_topic_recovery")
        and (topic_meta.get("thread_id") or topic_meta.get("message_thread_id"))
        and not adapter._is_timeout_error(error_str)
        and any(marker in " ".join(error_str.lower().split()) for marker in (
            "thread not found", "topic deleted", "topic closed", "topic not found"))
    ):
        thread_id = topic_meta.get("thread_id") or topic_meta.get("message_thread_id")
        mark_stale = getattr(adapter, "_mark_dm_topic_stale", None)
        if callable(mark_stale):
            mark_stale(chat_id, thread_id)
        prune = getattr(adapter, "_prune_stale_dm_topic_binding", None)
        if callable(prune):
            # A state.db write: run it off the loop, still before the root-chat fallback send.
            await asyncio.to_thread(prune, chat_id, thread_id, metadata=topic_meta)
        root_meta = {key: value for key, value in topic_meta.items() if key not in {
            "thread_id", "message_thread_id", "direct_messages_topic_id",
            "telegram_direct_messages_topic_id", "telegram_reply_to_message_id",
            "reply_to_message_id", "telegram_dm_topic_reply_fallback",
            "telegram_dm_topic_created_for_send",
        }}
        root_meta["telegram_stale_topic_recovery"] = True
        partial = adapter._is_partial_delivery(result)
        if partial:
            root_content = ("A response was partially delivered before its Telegram topic was deleted. "
                            "The complete response remains in Hermes session history.")
        elif topic_meta.get("copy_block"):
            # A copy block moves to the root unchanged: a banner would break the exact text.
            root_content = content
        else:
            root_content = "Recovered response from a deleted Telegram topic:\n\n" + content
        logger.warning("[%s] Recovering completed reply from deleted Telegram topic chat=%s thread=%s",
                       adapter.name, chat_id, thread_id)
        recovered = await adapter.send(chat_id=chat_id, content=root_content, reply_to=None, metadata=root_meta)
        if recovered.success:
            recovered.raw_response = {
                **(recovered.raw_response or {}), "thread_fallback": True, "requested_thread_id": thread_id,
            }
        return recovered

    return None
