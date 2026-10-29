"""Ledger admission for payloads safely unsent by cron's standalone fallback."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cron.scheduler_delivery import _TargetDelivery

logger = logging.getLogger("cron.scheduler")


def queue_for_live_reconnect(
    t: _TargetDelivery, content: str, media_files: list, delivery_errors: list,
    *, copy_blocks: list[str] | None = None,
) -> None:
    """Hand a payload the live lane rejected as reconnect-only (``send_path_degraded``) and the
    standalone lane then failed to send to the delivery ledger, as a failed reconnect-only row
    owned by the adapter that rejected it: the post-reconnect sweep redelivers it (#125363). Only
    reached after standalone failed, so nothing was sent and a replay cannot duplicate. The ledger
    carries text only; dropped attachments are reported."""
    try:
        from gateway.delivery_ledger import (
            compute_obligation_id, is_reconnect_only, ledger_enabled, mark_failed, record_obligation)
        if not is_reconnect_only(t.live_error) or not ledger_enabled():
            return
        session_key = f"cron:{t.platform_name}:{t.chat_id}" + (f":{t.thread_id}" if t.thread_id else "")
        job_ref = f"job:{t.job.get('id', '?')}"
        # One row per logical message, so a replay that lands some and fails one retries only
        # that one. Copy rows keep their markers so replay re-extracts and sends them plain.
        rows = [(job_ref, content)] if content else []
        from gateway.copy_blocks import wrap_copy_block
        rows += [(f"{job_ref}#copy{index}", wrap_copy_block(block))
                 for index, block in enumerate(copy_blocks or [])]
        for message_ref, queued_content in rows:
            obligation_id = compute_obligation_id(session_key, message_ref, queued_content)
            record_obligation(
                obligation_id=obligation_id, session_key=session_key, platform=t.platform_name,
                chat_id=str(t.chat_id), thread_id=t.thread_id, content=queued_content,
                adapter_profile=getattr(getattr(t.transport, "adapter", None), "_owner_profile", None))
            mark_failed(obligation_id, str(t.live_error))
    except Exception:
        logger.warning("Job '%s': could not queue %s for post-reconnect redelivery",
                       t.job.get("id"), t.where, exc_info=True)
        return
    row_count = len(rows)
    row_label = "row" if row_count == 1 else "rows"
    note = f"queued {row_count} delivery {row_label} for {t.where} for redelivery once the live adapter reconnects"
    if media_files:
        note += f" ({len(media_files)} attachment(s) not queued)"
    logger.warning("Job '%s': %s", t.job.get("id"), note)
    delivery_errors.append(note)
