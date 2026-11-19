"""Push one stream tick without changing boundary/finalization ordering."""

async def push_tick_update(self, tick):
    from gateway.stream_consumer import asyncio
    if self._should_edit(tick) and (
        self._accumulated or (self._use_native_streaming and self._tool_progress_active)
    ):
        # Seal first: it clears the message id, so a remainder still over the limit
        # is split again below. A plain first send would let the adapter split it and
        # adopt only the LAST chunk as the preview; the next seal then overwrites that
        # chunk with the head of the whole remainder (duplicated + lost text, #25349).
        sent_heads = await self._seal_overflow_heads(final=tick.got_done)
        if sent_heads and self._turn_reset():  # the sealing edit awaited: no split for a dead turn
            await self._abandon_native_stream()
            return "stop"
        # Overflow split.  Native streaming bypasses this: the adapter
        # truncates against the stream protocol's own limit.
        if not self._use_native_streaming and self._first_send_overflows():
            if await self._split_first_send(tick):
                return "stop"
            sent_heads = True
        # Each sealed or split head awaited a send: /stop or /new may have landed.
        if sent_heads and self._turn_reset():
            await self._abandon_native_stream()
            return "stop"
        if not self._use_native_streaming and self._first_send_overflows():
            # A head send failed: keep the full text for the fallback final, and
            # skip the boundary reset below that would clear it. Still yield: the
            # buffer stays over the debounce threshold, so `continue` alone re-enters
            # the split on every pass and spins the loop without ever sleeping.
            self._signal_flush(tick.flush_event)
            await asyncio.sleep(0.05)
            return "yield"
        # The split tail goes out now, so a commentary or tool boundary drained in
        # this tick still lands after it instead of being dropped.
        await self._push_update(tick)
        # /stop or /new may invalidate the turn while adapter I/O is awaited.
        # Never retry a refused final send for the dead generation.
        if self._turn_reset():
            await self._abandon_native_stream()
            return "stop"
    return None
