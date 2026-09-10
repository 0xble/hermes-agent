"""Turn-owned status delivery, including receipts arriving after final cleanup."""

import asyncio
import logging
import uuid
from collections import defaultdict
from contextvars import ContextVar

logger = logging.getLogger("gateway.run")
# Callback chains also contain always-run release hooks. Only temporary-bubble
# cleanup consumes this task-local outcome; no cross-session mutable registry.
final_delivery_succeeded = ContextVar("final_delivery_succeeded", default=True)


class StatusDelivery:
    """One turn, one adapter identity; never lend its status key to another turn."""

    def __init__(self, ctx, current_adapter):
        self.ctx = ctx
        self.current_adapter = current_adapter
        self.token = uuid.uuid4().hex
        self.closed = False
        self.cleaned = False
        self.owners = {}
        self.tasks = set()
        self.cleanup_failures = {}
        self.locks = defaultdict(asyncio.Lock)

    def live(self, adapter):
        return (
            not self.closed
            and self.ctx._run_still_current()
            and self.current_adapter() is adapter
        )

    def track(self, result, adapter, *, record_owner=True):
        if not self.ctx._cleanup_progress or not getattr(result, "success", False):
            return
        mid = getattr(result, "message_id", None)
        if not mid:
            return
        mid = str(mid)
        if record_owner:
            self.owners[mid] = adapter
        self.ctx._cleanup_msg_ids.append(mid)
        if self.cleaned:
            task = asyncio.create_task(self.delete(adapter, mid))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def delete(self, adapter, mid):
        """Bounded cleanup on the receipt's transport, not a replacement bot.

        Telegram's shared gate may defer expendable deletion without issuing a
        request. Preserve that distinction and give finals priority while retrying
        briefly; permanent failure remains visible rather than reported as clean.
        """
        delete = getattr(adapter, "_delete_status_message", None) or adapter.delete_message
        reason = "returned_false"
        try:
            # Leave room for Telegram's ordinary six-second group send gap.
            async with asyncio.timeout(10):
                for attempt in range(3):
                    result = await delete(self.ctx.source.chat_id, mid)
                    if result is True:
                        self.cleanup_failures.pop(mid, None)
                        logger.info("Temp receipt deleted for session %s generation %s: %s",
                                    self.ctx.session_key, self.ctx.run_generation, mid)
                        return True
                    if result is not None:
                        break
                    reason = "scheduler_deferred"
                    if attempt < 2:
                        await asyncio.sleep(1)
        except Exception as exc:
            reason = type(exc).__name__
        self.cleanup_failures[mid] = reason
        logger.warning("Temp receipt cleanup failed for session %s generation %s: %s:%s",
                       self.ctx.session_key, self.ctx.run_generation, mid, reason)
        return False

    async def progress_send(self, adapter, content, *, metadata=None):
        """One shielded progress send; keep its exact receipt owner after cancellation.

        No retries here: an exception or missing receipt does not prove rejection.
        The producer owns its attempted-send latch. Late receipts use the same
        cleanup path as ordinary receipts, including after final delivery.
        """
        if not self.live(adapter):
            return None

        async def send_owned():
            # A delayed producer can run under a different topic's task context.
            # Bind only in this child task, never mutate the caller's context.
            from gateway.session_context import set_session_vars, clear_session_vars
            source = self.ctx.source
            tokens = set_session_vars(
                platform=str(getattr(source.platform, "value", source.platform)),
                chat_id=source.chat_id, thread_id=getattr(source, "thread_id", "") or "",
                session_key=self.ctx.session_key or "", session_id=self.ctx.session_id or "",
                message_id=getattr(self.ctx, "event_message_id", "") or "",
            )
            try:
                result = await adapter.send(
                    chat_id=source.chat_id, content=content,
                    metadata=dict(metadata) if metadata else None)
                self.track(result, adapter)
                return result
            finally:
                clear_session_vars(tokens)

        receipt = asyncio.create_task(send_owned())
        self.tasks.add(receipt)  # strong reference until the transport settles

        def received(task):
            self.tasks.discard(task)
            if not task.cancelled():
                task.exception()  # consume errors even if the producer was cancelled

        receipt.add_done_callback(received)
        return await asyncio.shield(receipt)

    async def send(self, adapter, chat_id, event_type, content, metadata):
        from gateway.run import _send_or_update_status_coro

        async with self.locks[event_type]:
            if not self.live(adapter):
                return
            # Recheck inside the lock: a queued callback may outlive this turn.
            result = await _send_or_update_status_coro(
                adapter,
                chat_id,
                f"{self.token}:{event_type}",
                content,
                dict(metadata) if metadata else None,
            )
            self.track(result, adapter)
            return result
