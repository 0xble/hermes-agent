"""Telegram outbound send policy shared by the gateway adapter and the standalone sender
(``hermes send``, the ``send_message`` tool, standalone cron delivery): the push-notification
mode and the visually-empty guard, so both lanes make the same decision for the same text."""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

_NOTIFICATION_MODES = ("all", "important")

# Code points that render as nothing: zero-width space/non-joiner/joiner, word joiner, invisible
# operators, BOM, soft hyphen, Mongolian vowel separator, and the bidi marks/embeddings/isolates.
# A message made only of these (plus whitespace) passes ``str.strip()`` and lands as an empty bubble.
_INVISIBLE = str.maketrans("", "", (
    "\u200b\u200c\u200d\u2060\u2061\u2062\u2063\u2064\ufeff\u00ad\u180e"
    "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"))


def is_visually_empty(text: str | None) -> bool:
    """True when ``text`` has no visible glyph (empty, whitespace, or invisible code points only)."""
    return not text or not text.translate(_INVISIBLE).strip()


def resolve_notifications_mode() -> str:
    """``all`` or ``important``: ``HERMES_TELEGRAM_NOTIFICATIONS``, else the active profile's
    ``display.platforms.telegram.notifications``; unknown values warn and use ``important``."""
    mode = os.getenv("HERMES_TELEGRAM_NOTIFICATIONS", "").strip().lower()
    if not mode:
        from hermes_cli.config import cfg_get, load_config_readonly
        raw = cfg_get(load_config_readonly(), "display", "platforms", "telegram", "notifications")
        mode = "" if raw is None else str(raw).strip().lower()
    if not mode:
        return "important"
    if mode not in _NOTIFICATION_MODES:
        logger.warning("Unknown telegram notifications mode '%s', defaulting to 'important' (valid: %s)",
                       mode, ", ".join(_NOTIFICATION_MODES))
        return "important"
    return mode


def notification_kwargs(mode: str, notify: bool | None) -> dict:
    """Bot API kwargs for one send: silent under ``important`` unless the caller asks to ``notify``."""
    return {} if mode != "important" or notify else {"disable_notification": True}


def _standalone_adapter(bot, extra: dict):
    """An unconnected ``TelegramAdapter`` over the standalone ``bot`` (no polling, no lock), so an
    out-of-process send reuses the gateway's gates, routing and fallbacks verbatim."""
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter
    adapter = TelegramAdapter(PlatformConfig(enabled=True, extra=dict(extra or {})))
    adapter._bot = bot
    adapter._notifications_mode = resolve_notifications_mode()
    return adapter


def _metadata(thread_id, notify) -> dict:
    return {**({"thread_id": str(thread_id)} if thread_id is not None else {}), **({"notify": True} if notify else {})}


async def send_rich_standalone(bot, extra: dict, chat_id, content: str, *, thread_id=None, notify=None,
                               thread_kwargs=None):
    """One standalone rich send using the gateway's eligibility, payload and refusal rules.

    Only a definite stale-topic refusal or one short flood wait permits a repeated rich
    request. Ambiguous failures stop before legacy text or attachments. ``thread_kwargs``
    is the caller's routing, so dropping a stale topic also affects any legacy fallback.
    """
    from plugins.platforms.telegram.adapter import (
        _FLOOD_INLINE_WAIT_CAP_SECS, _normalize_dollar_entities,
        _redact_telegram_error_text, _telegram_retry_after)
    from gateway.platforms.helpers import send_long_reply_as_file
    from plugins.platforms.telegram.rich_parts import rich_part_count, send_rich_parts

    try:
        adapter = _standalone_adapter(bot, extra)
        content = _normalize_dollar_entities(content)
        # Probe the legacy eligibility hook before the richer split path. Older adapters and
        # callers may not expose the newer helper methods; an unavailable hook is a safe
        # signal to let _send_telegram use its MarkdownV2 fallback.
        try:
            adapter._should_attempt_rich(content)
        except (AttributeError, TypeError) as exc:
            logger.warning("Telegram rich adapter unavailable, falling back to legacy: %s",
                           _redact_telegram_error_text(exc))
            return None
        if (
            not adapter._rich_delivery_enabled()
            or getattr(adapter, "_rich_send_disabled", False)
            or not adapter._needs_rich_rendering(content)
            or not adapter._rich_content_ok(content, check_size=False)
        ):
            return None
    except Exception as exc:  # health: allow BLE001 -- no request has started; safe legacy fallback
        logger.warning("Telegram rich adapter unavailable, falling back to legacy: %s", _redact_telegram_error_text(exc))
        return None
    metadata = _metadata(thread_id, notify)
    request = adapter._request_rich_message
    flood_retried = False

    async def request_with_definite_refusal_retry(payload):
        # Do not put this policy around the entire split: earlier parts may
        # already have landed when the current part is rejected.
        nonlocal flood_retried
        while True:
            try:
                return await request(dict(payload))
            except Exception as exc:  # health: allow BLE001 -- only definite refusals permit another request
                wait = _telegram_retry_after(exc)
                if wait is not None and not flood_retried and wait <= _FLOOD_INLINE_WAIT_CAP_SECS:
                    flood_retried = True
                    logger.warning("Telegram rich send rate-limited, retrying once in %.1fs", wait)
                    await asyncio.sleep(max(wait, 0.0))
                    continue
                if (adapter._is_thread_not_found_error(exc)
                        and payload.get("message_thread_id") is not None
                        and payload.get("direct_messages_topic_id") is None):
                    logger.warning("Thread %s not found in rich Telegram send, retrying without message_thread_id",
                                   payload.pop("message_thread_id"))
                    metadata.pop("thread_id", None)
                    if thread_kwargs is not None:
                        thread_kwargs.pop("message_thread_id", None)
                    continue
                raise

    adapter._standalone_rich_request = request_with_definite_refusal_retry
    if rich_part_count(adapter, content, metadata) is None:
        return None
    as_file = await send_long_reply_as_file(adapter, str(chat_id), content, metadata=metadata)
    return as_file or await send_rich_parts(adapter, str(chat_id), content, None, metadata)


async def send_album_standalone(bot, extra: dict, chat_id, paths: list[str], *, thread_id=None, notify=None):
    """Photos as native albums (``sendMediaGroup``, 10 per chunk) through the adapter's album sender.
    A refused chunk and every photo after it are handed back in order in ``raw_response["refused"]``
    (local paths) for the caller's own per-file upload, which reports each failure; a lost reply is ``ambiguous`` and never resent."""
    from urllib.parse import quote, unquote
    from plugins.platforms.telegram.telegram_album import send_photo_album
    adapter = _standalone_adapter(bot, extra)
    # The caller hands absolute, existence-checked paths; quote() matches the album sender's unquote().
    images = [("file://" + quote(p), None) for p in paths]
    result = await send_photo_album(adapter, str(chat_id), images, _metadata(thread_id, notify), per_photo_fallback=False)
    if result.raw_response:
        result.raw_response["refused"] = [unquote(url[7:]) for url, _ in result.raw_response["refused"]]
    return result
