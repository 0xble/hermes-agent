"""Typed tool-progress classification and per-turn send acceptance."""

import logging
from typing import Any

from gateway.platforms.base import OUTBOUND_PROGRESS, OUTBOUND_TOOL_PROGRESS, SendResult, outbound_class

logger = logging.getLogger("gateway.run")


class _ToolProgressLine(str):
    """A real tool-call event, kept distinct from cosmetic thinking text."""


async def send_progress_text(state: Any, ctx: Any, text: str) -> SendResult:
    """Protect only the first accepted tool send, including durable or partial acceptance."""
    kind = (OUTBOUND_TOOL_PROGRESS if isinstance(text, _ToolProgressLine)
            and not state.tool_progress_sent else OUTBOUND_PROGRESS)
    formatter = getattr(type(state.adapter), "format_progress_text", None)
    if kind == OUTBOUND_TOOL_PROGRESS and formatter is not None:
        text = formatter(state.adapter, ctx.source.chat_id, text)
    with outbound_class(kind):
        result = await state.adapter.send(
            chat_id=ctx.source.chat_id, content=text, reply_to=ctx._progress_reply_to, metadata=ctx._progress_metadata,
        )
    if kind == OUTBOUND_TOOL_PROGRESS and (result.success or (result.raw_response or {}).get("partial_overflow")):
        state.tool_progress_sent = True
    return result


async def edit_progress_message(state: Any, ctx: Any, message_id: str, content: str) -> SendResult:
    kwargs = {"chat_id": ctx.source.chat_id, "message_id": message_id, "content": content}
    if getattr(state.adapter, "REQUIRES_EDIT_FINALIZE", False):
        kwargs["finalize"] = True
    if state._edit_accepts_metadata:
        kwargs["metadata"] = ctx._progress_metadata
    with outbound_class(OUTBOUND_PROGRESS):
        result = await state.adapter.edit_message(**kwargs)
    if (result.raw_response or {}).get("skipped"):
        state.skipped_edits += 1
        reason_fn = getattr(type(state.adapter), "progress_edit_pause_reason", None)
        if not state.progress_pause_logged and reason_fn is not None:
            reason = reason_fn(state.adapter, ctx.source.chat_id)
            if reason:
                logger.info("progress edits paused for %s: %s", ctx.source.chat_id, reason)
                state.progress_pause_logged = True
    return result
