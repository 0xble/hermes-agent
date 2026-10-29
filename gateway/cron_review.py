"""Local control-socket admission for invisible cron review turns."""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
import uuid

logger = logging.getLogger(__name__)

# How often a held review rechecks whether its subject topic has gone idle.
_IDLE_POLL_SECONDS = 5.0
# A review admitted with less budget than this would only time out unseen.
_MIN_REVIEW_SECONDS = 60.0


def is_review_event(event):
    metadata = getattr(event, "metadata", None)
    return (getattr(event, "internal", False) is True and isinstance(metadata, dict)
            and metadata.get("_cron_review_capture") is not None)


def fail_busy_review(event) -> bool:
    """Fail a review that reached a busy session; return whether it was a review.

    Every busy entry point calls this so a review is never queued, merged, or used
    to interrupt a live turn.
    """
    if not is_review_event(event):
        return False
    future = event.metadata["_cron_review_capture"]
    if not future.done():
        future.set_exception(RuntimeError("review subject is busy; use original report"))
    return True


def private_turn_response(event, agent_result, response=None):
    from gateway.warning_notifications import diagnostic_wake_muted
    if is_review_event(event):
        _missing = object()
        captured = getattr(event, "_agent_final_response", _missing)
        if captured is _missing:
            captured = agent_result.get("final_response")
        if captured is None:
            captured = response
        return True, str(captured or "")
    return diagnostic_wake_muted(event), None


def defer_review_followup(runner, ctx, adapter, event, text):
    """Human follow-ups need their own event, rather than the review's capture."""
    if not (ctx.event_metadata or {}).get("_cron_review_capture") or not (event or text):
        return event, text
    from gateway.platforms.event import MessageEvent
    event = event or MessageEvent(text=text, source=ctx.source, internal=False,
                                 reply_expected=True)
    existing = adapter._pending_messages.get(ctx.session_key)
    if existing is not None:
        runner._session_state(ctx.session_key).conversation.queued_events.insert(0, existing)
    adapter._pending_messages[ctx.session_key] = event
    return None, None


class ReviewCapture:
    """Keep captured turn output and errors off all adapter delivery paths."""
    def __init__(self, event):
        self.future = (getattr(event, "metadata", None) or {}).get("_cron_review_capture") if is_review_event(event) else None

    def finish(self, value="", error=None):
        if self.future is None or self.future.done():
            return
        if error is None:
            self.future.set_result(value or "")
        else:
            self.future.set_exception(error)

    def capture_reply(self, response, event=None):
        if self.future is None:
            return response
        # Streaming turns return None after the stream consumer has delivered the
        # final text. The event retains that exact final payload for the invisible
        # review caller, so capture it instead of treating a successful review as
        # empty.
        if not response and event is not None:
            streamed = getattr(event, "_streamed_final_response", None)
            if isinstance(streamed, str):
                response = streamed
        self.finish(response)
        return None

    def start_typing(self, adapter, event, interrupt_event, metadata):
        if self.future is None:
            return adapter._start_typing_refresh(event, interrupt_event, metadata)
        return None

    async def notify_error(self, adapter, event, error, session_key):
        if self.future is None:
            return await adapter._notify_turn_error(event, error, session_key)
        self.finish(error=error)
        return None


class CronReviewControl:
    def __init__(self, runner):
        self.runner = runner
        self.loop = asyncio.get_running_loop()
        self.lock = threading.Lock()
        self.tasks = {}

    def submit(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("cron-review requires a payload")
        key = str(payload.get("session_key") or "")
        session_id = str(payload.get("session_id") or "")
        text = str(payload.get("text") or "")
        seconds = float(payload.get("timeout_seconds") or 600)
        if not key or not session_id or not text or not 0 < seconds <= 86400:
            raise ValueError("invalid cron-review request")
        # Keep completed records for one hour, never retain full reports indefinitely.
        token = uuid.uuid4().hex
        task = asyncio.run_coroutine_threadsafe(self._review(key, session_id, text, seconds), self.loop)
        with self.lock:
            self.tasks = {k: v for k, v in self.tasks.items() if not v[1].done() or time.monotonic() - v[0] < 3600}
            self.tasks[token] = (time.monotonic(), task)
        return {"token": token}

    def result(self, params):
        with self.lock:
            record = self.tasks.get(str(params.get("token") or ""))
        if record is None:
            return {"status": "missing"}
        task = record[1]
        if not task.done():
            return {"status": "pending"}
        try:
            return {"status": "completed", "reply": task.result()}
        except BaseException as exc:
            logger.exception("Invisible cron review failed")
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}

    async def _review(self, key, session_id, text, seconds):
        from gateway.platforms.event import MessageEvent, MessageType
        from gateway.session_identity import replace_source
        from gateway.wake import admit_internal_event
        runner = self.runner
        if not runner._plugin_injection_accepting():
            raise RuntimeError("gateway not accepting review turns")
        entry = await runner.async_session_store.lookup_by_session_key(key)
        if entry is None or entry.origin is None or entry.session_id != session_id:
            raise RuntimeError("review subject session changed before admission")
        source = replace_source(runner._restored_source(entry))
        if not runner._plugin_injection_authorized(source, plugin_id="cron-review", target=key):
            raise RuntimeError("review subject is not authorized")
        adapter = runner._delivery_adapter_for(source)
        if adapter is None:
            raise RuntimeError("review subject adapter unavailable")
        # The subject topic is often mid-conversation when its report lands, so
        # wait inside the review budget for it to go idle rather than skipping
        # review outright; the on-time fallback still applies at the deadline.
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        waited = False
        while key in getattr(adapter, "_active_sessions", {}) or key in getattr(adapter, "_pending_messages", {}):
            if end - loop.time() < _IDLE_POLL_SECONDS + _MIN_REVIEW_SECONDS:
                raise RuntimeError("review subject is busy; use original report")
            waited = True
            await asyncio.sleep(_IDLE_POLL_SECONDS)
        if waited:
            # Anything resolved before the wait may be stale: the gateway may now be
            # draining (a drain is what idles a busy topic), the conversation may have
            # rotated its session, the adapter may have reconnected, or a grant changed.
            if not runner._plugin_injection_accepting():
                raise RuntimeError("gateway not accepting review turns")
            entry = await runner.async_session_store.lookup_by_session_key(key)
            if entry is None or entry.origin is None or entry.session_id != session_id:
                raise RuntimeError("review subject session changed while waiting")
            source = replace_source(runner._restored_source(entry))
            if not runner._plugin_injection_authorized(source, plugin_id="cron-review", target=key):
                raise RuntimeError("review subject is not authorized")
            adapter = runner._delivery_adapter_for(source)
            if adapter is None:
                raise RuntimeError("review subject adapter unavailable")
            if key in getattr(adapter, "_active_sessions", {}) or key in getattr(adapter, "_pending_messages", {}):
                raise RuntimeError("review subject is busy; use original report")
        seconds = end - loop.time()
        capture = concurrent.futures.Future()
        event = MessageEvent(
            text=text, message_type=MessageType.TEXT, source=source, internal=True,
            reply_expected=False, allow_gateway_control=False,
            metadata={"_cron_review_capture": capture, "gateway_session_key": key,
                      "gateway_session_id": session_id, "gateway_session_strict": True},
        )
        async with runner._plugin_injection_scope(source):
            await admit_internal_event(adapter, event)
            # Deadline expiry cancels only this capture, never a later human turn.
            return await asyncio.wait_for(asyncio.wrap_future(capture), seconds)
