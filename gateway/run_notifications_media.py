"""Media and queued-first-response delivery for GatewayRunner."""

from __future__ import annotations

import logging
from typing import Any, Optional
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


logger = logging.getLogger('gateway.run')


class GatewayNotificationsMixin:
    async def _deliver_media_from_response(
        self, response: str, event: MessageEvent, adapter, thread_metadata: Optional[dict[str, Any]] = None
    ) -> bool:
        """Deliver explicit attachments from an already-streamed response (text already delivered).
        EXPLICIT-ONLY, unlike the non-streaming path in ``gateway/platforms/base.py``: a bare local
        path in a streamed reply is shown text or stale inspected content, and promoting it sent
        files the model never asked for. MEDIA tags are NOT deduped against prior turns (a final-reply
        directive is a deliberate attach); stale auto-appended tags are deduped upstream.

        Explicit means ``MEDIA:`` directives and markdown/HTML image tags — the two contracts the
        non-streaming chain turns into attachments. See #20834 and #141806.
        Returns True when at least one upload was confirmed delivered.
        """
        from gateway.run_notifications import (
            Path, _IMAGE_EXTS, _VIDEO_EXTS, _log_suppressed, _media_send_succeeded, logging,
        )
        from urllib.parse import quote as _quote
        delivered_any = False
        with _log_suppressed(logging.WARNING, "Post-stream media extraction failed: %s"):
            # Capture [[as_document]] before extract_media strips it: images then go via send_document.
            force_document_attachments = "[[as_document]]" in response
            from gateway.platforms.base import BasePlatformAdapter, should_send_media_as_audio
            media_files, cleaned = adapter.extract_media(response)
            media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
            # Strip image URLs (parity with the non-streaming chain); no extract_local_files here.
            # Do NOT deduplicate explicit MEDIA tags against prior turns here (#73771). This rescan is
            # already EXPLICIT-ONLY (see docstring): a MEDIA: directive in the final streamed reply is the
            # model deliberately attaching a file — including a user-requested resend. Stale auto-appended
            # tags are deduped upstream in _collect_auto_append_media_tags with history_media_paths. Mirrors
            # the same filter removal on the non-streaming path in gateway/platforms/base.py. Bare local
            # paths in an already-streamed reply are text the user has seen (or stale inspected content),
            # not an attachment request.
            # A markdown/HTML image tag IS an attachment request, though — the non-streaming chain turns it
            # into a native attachment, so post-stream it has to be delivered here too. Discarding the
            # extraction left a streamed reply that showed the tag as text and never sent the image
            # (#141806).
            url_images, _ = adapter.extract_images(cleaned)
            _thread_meta = (
                dict(thread_metadata)
                if thread_metadata is not None
                else self._thread_metadata_for_source(event.source, self._reply_anchor_for_event(event))
            )
            chat_id = event.source.chat_id
            # Images go out as one batch (e.g. Signal's multi-attachment RPC) unless [[as_document]].
            def _is_photo(media_path: str, is_voice: bool) -> bool:
                ext = Path(media_path).suffix.lower()
                return ext in _IMAGE_EXTS and not is_voice and not force_document_attachments

            image_paths = [p for p, v in media_files if _is_photo(p, v)]
            non_image_media = [(p, v) for p, v in media_files if not _is_photo(p, v)]
            # Local images and URL images go out in one batch so a platform that bundles native
            # multi-attachment sends (Signal) still gets a single call. ``[[as_document]]`` only
            # applies to local files: a URL has no file to hand to send_document.
            if image_paths or url_images:
                try:
                    images = [(f"file://{_quote(p)}", "") for p in image_paths]
                    images.extend(url_images)
                    _res = await adapter.send_multiple_images(chat_id=chat_id, images=images, metadata=_thread_meta)
                    delivered_any = delivered_any or _media_send_succeeded(_res)
                except Exception as e:
                    logger.warning("[%s] Post-stream image batch delivery failed: %s", adapter.name, e)
            for media_path, is_voice in non_image_media:
                try:
                    ext = Path(media_path).suffix.lower()
                    if should_send_media_as_audio(event.source.platform, ext, is_voice=is_voice):
                        _res = await adapter.send_voice(
                            chat_id=chat_id, audio_path=media_path, metadata=_thread_meta, is_voice=is_voice,
                        )
                        delivered_any = delivered_any or _media_send_succeeded(_res)
                    elif ext in _VIDEO_EXTS:
                        _res = await adapter.send_video(chat_id=chat_id, video_path=media_path, metadata=_thread_meta)
                        delivered_any = delivered_any or _media_send_succeeded(_res)
                    else:
                        _res = await adapter.send_document(chat_id=chat_id, file_path=media_path, metadata=_thread_meta)
                        delivered_any = delivered_any or _media_send_succeeded(_res)
                except Exception as e:
                    logger.warning("[%s] Post-stream media delivery failed: %s", adapter.name, e)
        return delivered_any


    async def _deliver_queued_first_response(
        self, response: str, source: SessionSource, adapter,
        metadata: Optional[dict[str, Any]] = None, event_message_id: Optional[str] = None,
        text_already_delivered: bool = False, deliver_media: bool = True, stream_consumer=None,
        session_key: Optional[str] = None, inbound_message_id: Optional[str] = None,
    ) -> bool:
        """Deliver a queued response using the normal text+attachment split.

        ``session_key`` lets the text send record a delivery-ledger obligation like the normal final
        send does, keyed on ``inbound_message_id`` (the raw inbound id, distinct from the
        ``event_message_id`` reply anchor); see ``_send_queued_final_text``. Without a key the send
        stays unledgered.

        Returns whether the caller may treat this turn's final as delivered. True: the stream had
        already delivered it, the reconcile edit landed, the send succeeded, or there was nothing
        textual to send. False: the send was REFUSED (flood control, dead transport) — the caller
        must leave the normal completion send as the fallback, or the user gets nothing. A connector
        DECLINE returns True: that destination is not approved and must not be re-sent."""
        from gateway.run_notifications import (
            BasePlatformAdapter, MessageEvent, _mark_notify_metadata, asyncio,
        )
        from gateway.run import _strip_response_attachments_for_direct_send
        note_event = MessageEvent(text="", source=source, message_id=event_message_id)
        delivered_confirmed = text_already_delivered
        from gateway.copy_blocks import split_copy_blocks_for
        _, copy_blocks = split_copy_blocks_for(adapter, response)
        if not text_already_delivered:
            text_content = _strip_response_attachments_for_direct_send(response, adapter)
            if text_content:
                # Reconcile-by-edit first: a stream-sealed message already carries most of the answer;
                # a plain send here would duplicate it.
                _reconciled = False
                _sc_msg_id = getattr(stream_consumer, "message_id", None)
                if (
                    _sc_msg_id
                    and _sc_msg_id != "__no_edit__"
                    and not getattr(stream_consumer, "_turn_split_delivery", False)
                ):
                    try:
                        _edit_res = await adapter.edit_message(
                            chat_id=source.chat_id, message_id=_sc_msg_id, content=text_content, finalize=True,
                        )
                        if getattr(_edit_res, "success", False):
                            from gateway.reply_latency import current_receipt, record_first_reply
                            record_first_reply(current_receipt(), text_content, _edit_res)
                            _reconciled = True
                            delivered_confirmed = True
                            logger.info(
                                "Queued-lane final reconciled by editing message %s in place (no duplicate send).",
                                _sc_msg_id,
                            )
                        else:
                            # P5(b): a DECLINE is not "editing unavailable". The
                            # send below re-delivers the whole response to the
                            # chat the connector just refused.
                            from gateway.relay.egress import declined_send

                            if declined_send(_edit_res):
                                logger.warning(
                                    "Queued-lane reconcile edit DECLINED by the "
                                    "connector's egress guard; not falling back "
                                    "to a send (the destination is not approved)."
                                )
                                return True
                    except Exception as _qe:
                        logger.debug("Queued-lane reconcile edit failed (%s); falling back to send.", _qe)
                if not _reconciled:
                    _sent = await self._send_queued_final_text(
                        adapter, source, text_content, metadata, event_message_id, session_key,
                        inbound_message_id)
                    if not getattr(_sent, "success", False):
                        # The text never landed. Report it undelivered and skip the attachments too:
                        # the caller's normal completion send replays the whole response (text and
                        # its MEDIA: tags), so uploading here would duplicate every file.
                        return False
                    delivered_confirmed = True
        if copy_blocks and session_key and isinstance(adapter, BasePlatformAdapter):
            from gateway.reply_latency import current_receipt
            queued_copy_event = MessageEvent(
                text="", source=source, message_id=event_message_id, ledger_message_id=inbound_message_id)
            queued_copy_event._reply_latency_receipt = current_receipt()
            copy_results = []
            await adapter._send_copy_blocks(
                queued_copy_event, session_key, copy_blocks, _mark_notify_metadata(metadata),
                copy_results.append)
            if any(getattr(result, "success", False) for result in copy_results):
                delivered_confirmed = True
            if any(not getattr(result, "success", False) for result in copy_results):
                from gateway.delivery_ledger import ledger_enabled
                if not await asyncio.to_thread(ledger_enabled):
                    # No ledger owns the refused blocks. With nothing landed, the caller's
                    # whole-response fallback is the only retry; after a partial delivery it would
                    # duplicate, so the gap is reported and the turn stops here.
                    if not delivered_confirmed:
                        return False
                    logger.error(
                        "Queued-lane copy block delivery incomplete for %s and no delivery ledger "
                        "is enabled; the undelivered blocks are lost.", source.chat_id)
                else:
                    # Each block has its own ledger row, so a refused block is redelivered from
                    # the ledger. A whole-response fallback here would resend delivered parts.
                    logger.warning(
                        "Queued-lane copy block delivery incomplete for %s; ledger redelivery owns the rest.",
                        source.chat_id)
        else:
            for copy_block_index, copy_block in enumerate(copy_blocks):
                copy_metadata = dict(metadata or {})
                copy_metadata["copy_block"] = True
                copy_metadata["copy_block_index"] = copy_block_index
                copy_metadata["plain"] = True
                _sent = await self._send_queued_final_text(
                    adapter, source, copy_block, copy_metadata, event_message_id, session_key, inbound_message_id)
                if not getattr(_sent, "success", False):
                    logger.warning("Queued-lane copy block send failed to %s: %s", source.chat_id,
                                   getattr(_sent, "error", None) or "no result")
                    # Only a turn where nothing landed may fall back to the whole-response send;
                    # after a partial delivery that resend would duplicate what already arrived.
                    if not delivered_confirmed:
                        return False
                    break
                delivered_confirmed = True
        if (delivered_confirmed and session_key
                and hasattr(adapter, "_reconcile_restart_note_after_delivery")):
            if not hasattr(note_event, "_restart_note_marker_api_available"):
                await adapter._capture_restart_note_marker(note_event, session_key)
            await adapter._reconcile_restart_note_after_delivery(note_event, session_key)
        # they succeeded — mirrors the ``not agent_result.get("failed")`` completed-turn guard.
        if not deliver_media:
            return True
        # Attachments come only from text outside copy blocks: a body is literal text.
        from gateway.copy_blocks import strip_copy_blocks
        media_delivered = await self._deliver_media_from_response(
            strip_copy_blocks(response), MessageEvent(text="", source=source, message_id=event_message_id), adapter,
            thread_metadata=metadata,
        )
        # Attachment-only answer: no text was sent, so the note is reconciled only once an upload
        # is confirmed (same ownership-checked path as the text branch above).
        if (media_delivered and not delivered_confirmed and session_key
                and hasattr(adapter, "_reconcile_restart_note_after_delivery")):
            if not hasattr(note_event, "_restart_note_marker_api_available"):
                await adapter._capture_restart_note_marker(note_event, session_key)
            await adapter._reconcile_restart_note_after_delivery(note_event, session_key)
        return True


    async def _send_queued_final_text(
        self, adapter, source: SessionSource, text_content: str, metadata: Optional[dict[str, Any]],
        event_message_id: Optional[str], session_key: Optional[str],
        inbound_message_id: Optional[str] = None,
    ):
        """Send a queued-lane final through the same ledger bracket as the normal final
        (``send_final_ledgered``). This lane used to call ``adapter.send`` bare and discard the
        result, so a final refused here (flood control, a transport that had just died) left no
        ledger row and was gone for good. The ledger identity is the raw inbound message id;
        ``event_message_id`` is only the reply anchor, which is None wherever replies are not used
        (Telegram forum topics, Slack reaction handoffs) and so cannot identify the turn; with no
        inbound id the ledger falls back to the event's own (empty) message id. Adapters without
        the base contract and sends without a session key keep the plain send."""
        from gateway.run_notifications import BasePlatformAdapter, MessageEvent, _mark_notify_metadata
        if session_key and isinstance(adapter, BasePlatformAdapter):
            result, _ = await adapter.send_final_ledgered(
                MessageEvent(text="", source=source, ledger_message_id=inbound_message_id),
                session_key, text_content, _mark_notify_metadata(metadata), reply_to=event_message_id)
        else:
            result = await adapter.send(source.chat_id, text_content, metadata=metadata)
        from gateway.reply_latency import current_receipt, record_first_reply
        record_first_reply(current_receipt(), text_content, result)
        if not getattr(result, "success", False):
            logger.warning(
                "Queued-lane final send to %s failed: %s", getattr(source, "chat_id", "?"),
                getattr(result, "error", None) or "no result")
        return result
