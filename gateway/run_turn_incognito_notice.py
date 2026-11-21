"""One visible line after a restart drops the private RAM tail."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

INCOGNITO_RESTART_NOTICE = (
    "Hermes restarted, so the private messages before this were dropped. Incognito is still on."
)

# Platform send() implementations return SendResult for ordinary failures. These are the
# transport and adapter errors that can still escape a best-effort notice send.
_NOTICE_SEND_ERRORS = (OSError, RuntimeError, TimeoutError, ValueError)


def incognito_restart_notice(store, session_entry, response) -> str | None:
    """Return the restart notice when this reply should carry it, else None.

    Calling this consumes the pending mark. Do not compare the reply with
    ``final_response``: streaming, interruption, and marker stripping already
    make those strings differ.
    """
    if not isinstance(response, str) or not response.strip():
        return None
    from gateway.response_filters import is_intentional_silence_response
    if is_intentional_silence_response(response):
        return None
    session_key = getattr(session_entry, "session_key", None)
    mark = getattr(store, "mark_incognito_restart_notice_sent", None)
    if not session_key or not callable(mark) or mark(session_key) is not True:
        return None
    return INCOGNITO_RESTART_NOTICE


def prepend_incognito_restart_notice(store, session_entry, response):
    """Prepend the notice once, or return *response* unchanged."""
    notice = incognito_restart_notice(store, session_entry, response)
    if notice is None:
        return response
    body = response or ""
    return notice if not body else f"{notice}\n{body}"


async def send_streamed_incognito_restart_notice(adapter, chat_id, notice, metadata) -> None:
    """Send exactly the notice. The streamed body was already delivered."""
    if not notice or adapter is None:
        return
    try:
        await adapter.send(chat_id, notice, metadata=metadata)
    except _NOTICE_SEND_ERRORS as exc:
        logger.debug("streamed incognito restart notice send failed: %s", exc, exc_info=True)


async def apply_streamed_incognito_restart_notice(runner, session_entry, response, adapter, source, event):
    """Prepend and send the notice only when a restart actually dropped the private tail."""
    notice = incognito_restart_notice(getattr(runner, "session_store", None), session_entry, response)
    if notice is None:
        return response
    await send_streamed_incognito_restart_notice(
        adapter, getattr(source, "chat_id", None), notice, runner._event_thread_metadata(event, source))
    body = response or ""
    return notice if not body else f"{notice}\n{body}"
