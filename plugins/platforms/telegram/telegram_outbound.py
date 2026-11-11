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
    from gateway.platforms.base import SendResult
    from plugins.platforms.telegram.adapter import (
        _FLOOD_INLINE_WAIT_CAP_SECS, _TEXT_SEND_DEADLINE, _await_with_thread_deadline,
        _normalize_dollar_entities, _redact_telegram_error_text, _telegram_retry_after)
    try:
        adapter = _standalone_adapter(bot, extra)
        content = _normalize_dollar_entities(content)
        if not adapter._should_attempt_rich(content):
            return None
    except Exception as exc:  # health: allow BLE001 -- no request has started; safe legacy fallback
        logger.warning("Telegram rich adapter unavailable, falling back to legacy: %s", _redact_telegram_error_text(exc))
        return None
    if thread_kwargs is None:
        routing = adapter._compute_single_send_routing(chat_id, None, _metadata(thread_id, notify), thread_id)
        if routing is None:
            return None
        _, thread_kwargs = routing
        thread_kwargs = {**{k: v for k, v in thread_kwargs.items() if v is not None},
                         **notification_kwargs(adapter._notifications_mode, notify)}
    payload = {**adapter._rich_payload_base(chat_id, content), **thread_kwargs}
    flood_retried = False
    while True:
        try:
            response = await _await_with_thread_deadline(
                bot.do_api_request("sendRichMessage", api_kwargs=dict(payload)),
                timeout=_TEXT_SEND_DEADLINE, label="telegram-send", dump_on_blocked_loop=False)
            break
        except Exception as exc:  # health: allow BLE001 -- only definite refusals permit another send
            wait = _telegram_retry_after(exc)
            if wait is not None and not flood_retried and wait <= _FLOOD_INLINE_WAIT_CAP_SECS:
                flood_retried = True
                logger.warning("Telegram rich send rate-limited, retrying once in %.1fs: %s",
                               wait, _redact_telegram_error_text(exc))
                await asyncio.sleep(max(wait, 0.0))
                continue
            if "thread not found" in str(exc).lower() and payload.get("message_thread_id") is not None:
                logger.warning("Thread %s not found in rich Telegram send, retrying without message_thread_id",
                               payload.pop("message_thread_id"))
                thread_kwargs.pop("message_thread_id", None)
                continue
            if adapter._rich_rejected(exc, "sendRichMessage", "MarkdownV2"):
                return None
            return SendResult(success=False, error=_redact_telegram_error_text(exc))
    if isinstance(response, dict):
        message_id = response.get("message_id")
        if message_id is None:
            message_id = (response.get("result") or {}).get("message_id")
    else:
        message_id = getattr(response, "message_id", None)
    if message_id is not None:
        await adapter._record_rich_sent(chat_id, message_id, content)
    return SendResult(success=True, message_id=str(message_id) if message_id is not None else None)


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
