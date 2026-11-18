"""Pending-follow-up phases for gateway turn completion."""

from __future__ import annotations


def _drain_leftover_steer(
    self, result, adapter, source, session_key, pending_event, pending, *, logger,
):
    """Apply leftover /steer ordering without changing the pending event's identity."""
    # Leftover /steer (arrived after the last tool batch): deliver as the next user turn.
    leftover_steer = (result.get("pending_steer") or "").strip() if result else None
    if leftover_steer:
        if not pending and not pending_event:
            pending = leftover_steer
            logger.debug("Delivering leftover /steer as next turn: '%s...'", pending[:40])
        elif pending_event and adapter and session_key:
            # User steered during the turn, but a background event or queued message arrived.
            # Deliver the user's steer first and restore the pending event to the head of the queue.
            overflow = self._overflow_queue(session_key)
            if hasattr(adapter, "_pending_messages") and isinstance(adapter._pending_messages, dict):
                promoted = adapter._pending_messages.get(session_key)
                if promoted is not None:
                    if overflow is not None:
                        overflow.insert(0, promoted)
                    else:
                        self._session_state(session_key).conversation.queued_events.insert(0, promoted)
                adapter._pending_messages[session_key] = pending_event
            else:
                if overflow is not None:
                    overflow.insert(0, pending_event)
                else:
                    self._session_state(session_key).conversation.queued_events.insert(0, pending_event)
            pending_event = None
            pending = leftover_steer
            logger.debug(
                "Delivering leftover /steer before queued event for session %s: '%s...'",
                session_key, pending[:40],
            )
        elif pending and not pending_event:
            from gateway.platforms.base import MessageEvent
            # A deferred steer continues the active channel context, just like an
            # eventless follow-up. Reuse its pins without marking the user as internal.
            steer_prompt, steer_source = self._pinned_channel_inputs(
                session_key, None, source, internal=True,
            )
            steer_event = MessageEvent(
                text=leftover_steer, source=steer_source, channel_prompt=steer_prompt,
            )
            if session_key:
                self._enqueue_fifo(session_key, steer_event, adapter)
            logger.debug(
                "Enqueued leftover /steer behind pending message for session %s: '%s...'",
                session_key or "?", leftover_steer[:40],
            )
    return pending_event, pending
