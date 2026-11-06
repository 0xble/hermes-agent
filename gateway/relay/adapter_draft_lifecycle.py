"""Owned draft sealing and orphan cleanup for the relay adapter."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from gateway.platforms.base import SendResult
from gateway.relay.egress import decline_error, is_egress_decline, log_decline

logger = logging.getLogger("gateway.relay.adapter")


class RelayDraftLifecycleMixin:
    """Keep draft ownership and seal outcomes together across final and abandon paths."""

    async def _seal_open_draft(
        self,
        chat_id: str,
        content: str,
        metadata: Optional[dict[str, Any]],
        *,
        draft_key: Optional[str] = None,
    ) -> SendResult:
        """Convert the turn-final send into the sealing draft frame."""
        if draft_key is None:
            draft_key = self._draft_key(str(chat_id), metadata)
        draft_id = self._open_draft_by_chat.pop(draft_key)
        # Tombstone BEFORE the transport call: whatever the ack says, this draft_id
        # must never be re-armed by a straggler frame.
        self._sealed_draft_by_chat[draft_key] = draft_id
        self._evict_oldest(self._sealed_draft_by_chat)
        if self._transport is None:
            return SendResult(success=False, error="no transport")
        seal_frame = self._draft_frame(chat_id, draft_id, content, True, metadata)

        _seal_platform = self._platform_by_chat.get(str(chat_id))
        _transport = self._transport  # narrowed by the None-guard above

        async def _attempt() -> Optional[dict[str, Any]]:
            """One seal attempt; None means ambiguous (exception or lost ack)."""
            try:
                r = await _transport.send_outbound(seal_frame, platform=_seal_platform)
            except Exception as e:
                logger.warning("relay seal transport error (ambiguous): %s", e)
                return None
            if r.get("ambiguous"):
                logger.warning("relay seal ack lost (ambiguous): %s", r.get("error"))
                return None
            return r

        # Ambiguous outcomes retry the SAME idempotent frame once: the connector's
        # sealed-key tombstone returns the original stream ts for a repeated final
        # and never opens a second stream. Two consecutive ack losses on one socket
        # almost always mean the transport is down. Cancellation safety: the open
        # entry was popped and the tombstone written BEFORE the await; restore both
        # before re-raising so the later abandon pass can still seal the stream.
        try:
            result = await _attempt()
            if result is None:
                result = await _attempt()
        except asyncio.CancelledError:
            self._open_draft_by_chat[draft_key] = draft_id
            if self._sealed_draft_by_chat.get(draft_key) == draft_id:
                self._sealed_draft_by_chat.pop(draft_key, None)
            raise
        if result is None:
            # Same ambiguity contract as send_draft: the retry's ack was lost,
            # so the seal may have been applied. Marked explicitly rather than
            # left to text inference.
            return SendResult(
                success=False,
                error="draft seal ambiguous after retry (transport ack lost)",
                raw_response={"success": False, "ambiguous": True},
            )
        if result.get("success"):
            # The connector returns the stream's ts as the message identity.
            return SendResult(success=True, message_id=str(result.get("message_id") or "") or None)
        # P5(b): carry the structured body. Without it the caller cannot tell a
        # lane failure (fall through to a plain send, correct) from an
        # AUTHORIZATION decline (a plain send re-delivers the very content the
        # connector refused, to the same chat).
        if is_egress_decline(result):
            log_decline("draft_seal", chat_id, result)
        return SendResult(
            success=False,
            error=str(result.get("error") or decline_error(result) or "draft seal failed"),
            raw_response=result,
        )

    async def abandon_open_draft(
        self, chat_id: str, content: str, metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Seal an orphaned stream when its turn dies (/stop, /new, supersede), in
        place with ``content`` (the text already on screen) so the seal adds and
        claims nothing; otherwise the live indicator stays forever and the NEXT turn
        could inherit the armed interception state. Failure is reported, never raised."""
        draft_key = self._match_open_draft(str(chat_id), metadata)
        draft_id = (metadata or {}).get("_draft_id")
        if (draft_key is None or draft_id is None
                or self._open_draft_by_chat.get(draft_key) != draft_id):
            return SendResult(success=True)  # no proven owned draft — no-op
        try:
            return await self._seal_open_draft(chat_id, content, metadata, draft_key=draft_key)
        except Exception as e:
            logger.exception("relay abandon seal transport error")
            return SendResult(success=False, error=f"abandon seal transport error: {e}")

