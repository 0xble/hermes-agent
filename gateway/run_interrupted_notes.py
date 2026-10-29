"""Persisted destinations and acknowledged receipts for interruption notes."""

import logging

logger = logging.getLogger("gateway.run")


class InterruptionNotesMixin:
    def _record_interruption_note_receipt(self, session_key: str, note_id: str) -> None:
        # A pending token is a local reservation, not evidence that Telegram saw a message.
        if note_id.startswith("pending:"):
            return
        name = "_s2_note_ambiguous_keys" if note_id.startswith("sending:") else "_s2_note_delivered_keys"
        keys = getattr(self, name, None)
        if keys is None:
            keys = set()
            setattr(self, name, keys)
        keys.add(session_key)

    async def _shutdown_notification_target(self, session_key: str):
        """``(source, platform_str, chat_id, thread_id, profile)``: persisted origin > cached source >
        parsed key. ``profile`` is the owning profile from the source or the ``agent:<profile>:`` key
        namespace (``None`` = default) so the notice leaves through that profile's bot."""
        from gateway.run import _parse_session_key
        source = None
        try:
            store = getattr(self, "session_store", None)
            if store is not None:
                async_store = getattr(self, "async_session_store", None)
                if async_store is not None:
                    await async_store._ensure_loaded()
                entry = getattr(store, "_entries", {}).get(session_key)
                source = getattr(entry, "origin", None) if entry else None
        except Exception as e:
            logger.debug("Failed to load session origin for shutdown notification %s: %s", session_key, e)
        if source is None:
            source = self._get_cached_session_source(session_key)
        if source is not None:
            return source, source.platform.value, str(source.chat_id), source.thread_id, getattr(source, "profile", None)
        _parsed = _parse_session_key(session_key)
        if not _parsed:
            return None
        return None, _parsed["platform"], _parsed["chat_id"], _parsed.get("thread_id"), _parsed.get("profile")
