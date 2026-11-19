"""Queued-lane copy-block delivery and its no-duplicate fallback decision."""

async def deliver_queued_copy_blocks(self, adapter, source, session_key, copy_blocks,
                                    metadata, event_message_id, inbound_message_id, delivered_confirmed):
    from gateway.run_notifications import asyncio, BasePlatformAdapter, MessageEvent, _mark_notify_metadata, logger
    if copy_blocks and session_key and isinstance(adapter, BasePlatformAdapter):
        from gateway.reply_latency import current_receipt
        queued_copy_event = MessageEvent(
            text="", source=source, message_id=event_message_id, ledger_message_id=inbound_message_id)
        queued_copy_event._reply_latency_receipt = current_receipt()
        copy_results = []
        await adapter._send_copy_blocks(
            queued_copy_event, session_key, copy_blocks, _mark_notify_metadata(metadata),
            copy_results.append)
        if any(getattr(result, "success", False) for result in copy_results):
            delivered_confirmed = True
        if any(not getattr(result, "success", False) for result in copy_results):
            from gateway.delivery_ledger import ledger_enabled
            if not await asyncio.to_thread(ledger_enabled):
                # No ledger owns the refused blocks. With nothing landed, the caller's
                # whole-response fallback is the only retry; after a partial delivery it would
                # duplicate, so the gap is reported and the turn stops here.
                if not delivered_confirmed:
                    return False, False
                logger.error(
                    "Queued-lane copy block delivery incomplete for %s and no delivery ledger "
                    "is enabled; the undelivered blocks are lost.", source.chat_id)
            else:
                # Each block has its own ledger row, so a refused block is redelivered from
                # the ledger. A whole-response fallback here would resend delivered parts.
                logger.warning(
                    "Queued-lane copy block delivery incomplete for %s; ledger redelivery owns the rest.",
                    source.chat_id)
    else:
        for copy_block_index, copy_block in enumerate(copy_blocks):
            copy_metadata = dict(metadata or {})
            copy_metadata["copy_block"] = True
            copy_metadata["copy_block_index"] = copy_block_index
            copy_metadata["plain"] = True
            _sent = await self._send_queued_final_text(
                adapter, source, copy_block, copy_metadata, event_message_id, None, session_key, inbound_message_id)
            if not getattr(_sent, "success", False):
                logger.warning("Queued-lane copy block send failed to %s: %s", source.chat_id,
                               getattr(_sent, "error", None) or "no result")
                # Only a turn where nothing landed may fall back to the whole-response send;
                # after a partial delivery that resend would duplicate what already arrived.
                if not delivered_confirmed:
                    return False, False
                break
            delivered_confirmed = True
    return delivered_confirmed, True
