"""Own editable status messages and their cleanup for one gateway turn."""

import asyncio
import uuid
from collections import defaultdict
from contextlib import suppress


class StatusDelivery:
    """Fence status updates to their originating turn, topic and transport."""

    def __init__(self, ctx, current_adapter):
        self.ctx = ctx
        self.current_adapter = current_adapter
        self.token = uuid.uuid4().hex
        self.closed = False
        self.cleaned = False
        self.owners = {}
        self.tasks = set()
        self.locks = defaultdict(asyncio.Lock)

    def live(self, adapter):
        return not self.closed and self.ctx._run_still_current() and self.current_adapter() is adapter

    def track(self, result, adapter):
        if not self.ctx._cleanup_progress or not getattr(result, "success", False):
            return
        mid = getattr(result, "message_id", None)
        if not mid:
            return
        mid = str(mid)
        if self.cleaned:
            # The transport accepted the send but the final was delivered before its receipt.
            task = asyncio.create_task(self.delete(adapter, mid))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        else:
            self.owners[mid] = adapter
            self.ctx._cleanup_msg_ids.append(mid)

    async def delete(self, adapter, mid):
        with suppress(Exception):
            await asyncio.wait_for(adapter.delete_message(self.ctx.source.chat_id, mid), 5)

    async def send(self, adapter, chat_id, event_type, content, metadata):
        from gateway.run import _send_or_update_status_coro

        async with self.locks[event_type]:
            if not self.live(adapter):
                return
            # The token prevents a later turn reusing an earlier turn's editable bubble.
            # Thread identity additionally partitions the adapter's status cache.
            result = await _send_or_update_status_coro(
                adapter, chat_id, f"{self.token}:{event_type}", content,
                dict(metadata) if metadata else None,
            )
            self.track(result, adapter)
            return result
