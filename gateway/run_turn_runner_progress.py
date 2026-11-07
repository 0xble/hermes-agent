"""Typed tool-progress classification and per-turn send acceptance."""

from typing import Any

from gateway.platforms.base import OUTBOUND_PROGRESS, OUTBOUND_TOOL_PROGRESS, SendResult, outbound_class


class _ToolProgressLine(str):
    """A real tool-call event, kept distinct from cosmetic thinking text."""


async def send_progress_text(state: Any, ctx: Any, text: str) -> SendResult:
    """Protect only the first accepted tool send, including durable or partial acceptance."""
    kind = (OUTBOUND_TOOL_PROGRESS if isinstance(text, _ToolProgressLine)
            and not state.tool_progress_sent else OUTBOUND_PROGRESS)
    from gateway.reply_latency import delivery_scope, record_first_reply
    receipt = getattr(ctx, "_reply_latency_receipt", None)
    with outbound_class(kind), delivery_scope(receipt, "progress_send"):
        result = await state.adapter.send(
            chat_id=ctx.source.chat_id, content=text, reply_to=ctx._progress_reply_to, metadata=ctx._progress_metadata,
        )
    record_first_reply(receipt, text, result, kind="progress_send")
    if kind == OUTBOUND_TOOL_PROGRESS and (result.success or (result.raw_response or {}).get("partial_overflow")):
        state.tool_progress_sent = True
    return result
