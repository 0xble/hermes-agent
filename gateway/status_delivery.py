"""Turn-owned status delivery, including receipts arriving after final cleanup."""

import asyncio
import logging
import time
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
        self.pending_deletes = {}
        self.deleted = set()
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
        """True=absent, None=owned deferred work, False=terminal failure.

        A scheduler refusal issues no request and must not consume the receipt.
        Deadline retries belong to the adapter's existing shutdown task registry;
        they never hold the final-delivery callback open or switch transports.
        Like the existing status receipts, this queue is process-local: shutdown
        cancels it, and restart must not infer deletion authority from old logs.
        """
        async with self.locks[("delete", adapter, str(mid))]:
            return await self._delete_owned(adapter, mid)

    async def _delete_owned(self, adapter, mid):
        if not self.cleaned or not self.ctx._cleanup_progress:
            return False
        key = (adapter, str(mid))
        if key in self.deleted:
            return True
        if key in self.pending_deletes:
            return None
        result = await self._delete_once(adapter, mid)
        if result is not None:
            return result
        # Bound retained turn contexts during a prolonged transport outage.
        count = getattr(adapter, "_pending_status_delete_count", 0)
        if count >= 256:
            self._delete_failed(mid, "pending_capacity")
            return False
        adapter._pending_status_delete_count = count + 1
        task = asyncio.create_task(self._retry_delete(adapter, mid))
        self.pending_deletes[key] = task
        adapter._background_tasks.add(task)

        def settled(task):
            self.pending_deletes.pop(key, None)
            adapter._pending_status_delete_count -= 1
            adapter._background_tasks.discard(task)
            if not task.cancelled():
                task.exception()
        task.add_done_callback(settled)
        logger.info("Temp receipt cleanup deferred for session %s generation %s: %s",
                    self.ctx.session_key, self.ctx.run_generation, mid)
        return None

    def _delete_failed(self, mid, reason):
        self.cleanup_failures[mid] = reason
        logger.warning("Temp receipt cleanup failed for session %s generation %s: %s:%s",
                       self.ctx.session_key, self.ctx.run_generation, mid, reason)

    async def _delete_once(self, adapter, mid):
        delete = getattr(adapter, "_delete_status_message", None) or adapter.delete_message
        try:
            async with asyncio.timeout(10):
                result = await delete(self.ctx.source.chat_id, mid)
        except Exception as exc:
            self._delete_failed(mid, type(exc).__name__)
            return False
        if result is None:
            return None
        if result is True:
            self.deleted.add((adapter, str(mid)))
            self.cleanup_failures.pop(mid, None)
            logger.info("Temp receipt deleted for session %s generation %s: %s",
                        self.ctx.session_key, self.ctx.run_generation, mid)
            return True
        self._delete_failed(mid, "returned_false")
        return False

    async def _retry_delete(self, adapter, mid):
        # Telegram rejects messages older than 48h. This is a lifetime bound,
        # not a longer retry sleep: each wake follows the shared gate deadline.
        expires = time.monotonic() + 48 * 3600
        try:
            while True:
                remaining = expires - time.monotonic()
                if remaining <= 0:
                    self._delete_failed(mid, "deferred_expired")
                    return
                delay = getattr(adapter, "deletion_retry_after", lambda _: 1)(self.ctx.source.chat_id)
                await asyncio.sleep(min(remaining, max(1.0, delay)))
                if time.monotonic() >= expires:
                    self._delete_failed(mid, "deferred_expired")
                    return
                if await self._delete_once(adapter, mid) is not None:
                    return
        except asyncio.CancelledError:
            self._delete_failed(mid, "deferred_cancelled")
            raise

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
