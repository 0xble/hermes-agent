"""Ordered copy-block delivery and visible failure reporting."""

import logging
import time

# Retain the adapter's logger so all final-delivery diagnostics share one category.
logger = logging.getLogger("gateway.platforms.base")


async def _send_copy_blocks(
    self, event, session_key: str, copy_blocks: list[str], metadata: dict,
    record_delivery, *, attachments_pending: bool = False,
    is_ephemeral_response: bool = False, ephemeral_ttl: int = 0,
) -> None:
    """Deliver copy blocks in source order through the final delivery ledger, without pacing."""
    from gateway.platforms.base import SendResult, _reply_anchor_for_event

    previous_send_started = None
    for index, block in enumerate(copy_blocks):
        send_started = time.monotonic()
        if previous_send_started is not None:
            logger.debug(
                "[%s] Copy block %d/%d inter-message start gap: %.3fs",
                self.name, index, len(copy_blocks), send_started - previous_send_started)
        previous_send_started = send_started
        copy_metadata = dict(metadata)
        copy_metadata["copy_block"] = True
        copy_metadata["copy_block_index"] = index
        copy_metadata["plain"] = True
        deferred: list = []

        async def _defer_rest(refused, refusing_adapter, _index=index, _metadata=copy_metadata):
            # A later block must never overtake an undelivered earlier one: the rest wait
            # in the ledger behind the refused block, and redelivery keeps their order.
            deferred.extend(await self._defer_copy_blocks(
                event, session_key, copy_blocks, _index + 1, _metadata, refusing_adapter,
                str(refused.error or "send failed"), is_ephemeral_response=is_ephemeral_response))

        try:
            result, delivery_adapter = await self.send_final_ledgered(
                event, session_key, block, copy_metadata,
                reply_to=_reply_anchor_for_event(event),
                is_ephemeral_response=is_ephemeral_response,
                release_marker=(index == len(copy_blocks) - 1 and not attachments_pending),
                on_refused=_defer_rest,
            )
        except Exception as exc:
            result, delivery_adapter = SendResult(success=False, error=str(exc)), self
            try:
                await _defer_rest(result, delivery_adapter)
            except Exception:
                logger.exception("Copy followers could not be retained for session %s (%d/%d blocks)",
                                 session_key, len(copy_blocks) - index, len(copy_blocks))
        record_delivery(result)
        if ephemeral_ttl and ephemeral_ttl > 0 and result.success and result.message_id:
            delivery_adapter._schedule_ephemeral_delete(event.source.chat_id, result.message_id, ephemeral_ttl)
        if not getattr(result, "success", False):
            await notify_copy_failure(
                self, event, session_key, len(copy_blocks), len(copy_blocks) - index,
                result.error or "unknown delivery error", metadata)
            for held in deferred:
                record_delivery(held)
            return


async def notify_copy_failure(adapter, event, session_key, total, unsent, error, metadata):
    """Keep the ledger as recovery owner, but tell the user their reply is incomplete."""
    logger.error(
        "[%s] Copy blocks undelivered %d/%d for session %s chat %s: %s",
        adapter.name, unsent, total, session_key, event.source.chat_id, error)
    try:
        result = await adapter.send(
            chat_id=event.source.chat_id,
            content=f"{unsent} of {total} copy blocks could not be delivered. "
                    "This reply is incomplete.",
            metadata={**metadata, "plain": True, "notify": True})
        if not getattr(result, "success", False):
            logger.error("Copy failure notice undelivered for session %s (%d/%d blocks): %s",
                         session_key, unsent, total, getattr(result, "error", None))
    except Exception:
        logger.exception("Copy failure notice raised for session %s (%d/%d blocks)",
                         session_key, unsent, total)
