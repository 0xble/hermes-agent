"""Gateway auto-steer routing and original-event queue ownership."""
from __future__ import annotations

import asyncio
import logging
import time

from gateway.platforms.event import MessageType

logger = logging.getLogger(__name__)


def can_auto_steer(event) -> bool:
    from gateway.response_filters import is_agent_origin_text
    return (not event.internal and event.message_type == MessageType.TEXT
            and bool((event.text or "").strip())
            and not event.media_urls and not event.media_types
            and not (event.text or "").lstrip().startswith("/")
            and not is_agent_origin_text(event.text))


def stamp_busy_arrival(event) -> int:
    if not hasattr(event, "_busy_arrival_order"):
        event._busy_arrival_order = time.monotonic_ns()
    return event._busy_arrival_order


def _restore_arrival_order(runner, session_key, adapter) -> None:
    slot = adapter._pending_messages
    overflow = runner._overflow_queue(session_key)
    events = [slot[session_key], *(overflow or [])]
    # Untagged recovered entries predate this turn. Never stamp them at deferral time.
    events.sort(key=lambda event: getattr(event, "_busy_arrival_order", -1))
    slot[session_key] = events[0]
    if overflow is not None:
        overflow[:] = events[1:]


async def try_auto_steer(runner, event, session_key, agent) -> bool:
    from agent.steer_deferral import offer_auto_steer
    from gateway.run import _AGENT_PENDING_SENTINEL

    if not can_auto_steer(event) or agent is None or agent is _AGENT_PENDING_SENTINEL:
        return False
    stamp_busy_arrival(event)
    loop = asyncio.get_running_loop()
    turn = runner._session_state(session_key).turn
    adapter = runner._delivery_adapter_for(event.source)

    async def enqueue_original() -> bool:
        # This callback is run on the gateway loop, never on the tool worker. No await between the
        # ownership check and queue admission. A retired turn must not leak into its replacement.
        state = runner._peek_session_state(session_key)
        if (state is None or state.turn is not turn or turn.agent is not agent
                or getattr(agent, "_interrupt_requested", False) is True):
            return False
        if not runner._queue_or_replace_pending_event(session_key, event, preserve_event=True):
            return False
        _restore_arrival_order(runner, session_key, adapter)
        # Admission owns the original now. Transport cancellation must not leave its receipt
        # actionable, so acknowledge independently without suspending this ownership handoff.
        ack = runner._send_busy_ack_reply(event, adapter, "Queued after this.")
        task = None
        try:
            task = loop.create_task(ack, name="auto-steer-ack")
            runner._retain_background_task(task)
        except Exception:
            if task is None:
                ack.close()
            else:
                task.cancel()
            logger.debug("Failed to schedule auto-steer acknowledgment", exc_info=True)
        return True

    def handoff() -> bool:
        # No timeout: a timed-out future could enqueue later and make a retry double-deliver.
        if loop.is_closed():
            return False
        return asyncio.run_coroutine_threadsafe(enqueue_original(), loop).result()

    # Only the owning agent gets the receipt. Child agents cannot queue its original event.
    compression_active = await runner._session_has_compression_in_flight(session_key)
    if not offer_auto_steer(agent, runner._steer_text_with_origin(event.text, event), handoff,
                           redirect=not compression_active):
        return False
    runner._fold_into_running_turn(agent, session_key, event)
    return True
