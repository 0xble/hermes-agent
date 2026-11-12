"""Content-free topic creation failures and process-local outage suppression."""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any, Literal


_OUTAGE_CODE = "BOT_TOPIC_WRONG_HOME"
_COOLDOWN_SECONDS = 15 * 60
_FAILURE_MESSAGES = {
    "bot_unavailable": "Telegram's bot is unavailable right now. Continue in the current topic.",
    "threaded_mode_disabled": (
        "Telegram Threaded Mode is disabled. Enable Threaded Mode in BotFather before creating topics. "
        "Continue in the current topic."
    ),
    "duplicate": "Telegram reports that this topic already exists. Use the existing topic instead of retrying creation.",
    "server_refused": (
        "Telegram is refusing new topics right now (BOT_TOPIC_WRONG_HOME, a Telegram server-side outage). "
        "Existing topics still work. Continue in the current topic."
    ),
    "other": "Telegram could not create the topic. Continue in the current topic instead of retrying creation.",
}


@dataclass(frozen=True)
class TopicCreationFailure:
    """Only categories and known codes cross the incognito-safe request boundary."""

    kind: Literal["bot_unavailable", "threaded_mode_disabled", "duplicate", "server_refused", "other"]
    code: str | None = None


def topic_creation_error_message(adapter) -> str:
    failure = getattr(adapter, "_last_topic_creation_failure", None)
    if not isinstance(failure, TopicCreationFailure):
        return _FAILURE_MESSAGES["other"]
    message = _FAILURE_MESSAGES[failure.kind]
    if failure.kind == "server_refused":
        seconds = math.ceil(max(0, adapter._topic_creation_cooldown_until - time.monotonic()))
        if seconds:
            count, unit = (math.ceil(seconds / 60), "minute") if seconds >= 60 else (seconds, "second")
            message += f" Topic creation is paused for {count} {unit}{'s' if count != 1 else ''}. Do not retry until then."
    return message


def _classify_failure(error: Exception) -> TopicCreationFailure:
    error_text = str(error).lower()
    if _OUTAGE_CODE.lower() in error_text:
        return TopicCreationFailure("server_refused", _OUTAGE_CODE)
    if "topic_name_duplicate" in error_text or "already" in error_text:
        return TopicCreationFailure("duplicate")
    if "not a forum" in error_text or "forums_disabled" in error_text:
        return TopicCreationFailure("threaded_mode_disabled")
    return TopicCreationFailure("other")


async def create_dm_topic(
    adapter, chat_id: int, name: str, icon_color: int | None = None,
    icon_custom_emoji_id: str | None = None,
) -> int | None:
    """Return a thread id or None, retaining a safe reason for callers that need it."""
    from .adapter import logger, _redact_telegram_error_text

    if not adapter._bot:
        adapter._last_topic_creation_failure = TopicCreationFailure("bot_unavailable")
        return None
    if time.monotonic() < adapter._topic_creation_cooldown_until:
        adapter._last_topic_creation_failure = TopicCreationFailure("server_refused", _OUTAGE_CODE)
        return None
    try:
        kwargs: dict[str, Any] = {"chat_id": chat_id, "name": name}
        if icon_color is not None:
            kwargs["icon_color"] = icon_color
        if icon_custom_emoji_id:
            kwargs["icon_custom_emoji_id"] = icon_custom_emoji_id
        topic = await adapter._bot.create_forum_topic(**kwargs)
        thread_id = topic.message_thread_id
        adapter._last_topic_creation_failure = None
        logger.info("[%s] Created DM topic '%s' in chat %s -> thread_id=%s", adapter.name, name, chat_id, thread_id)
        return thread_id
    except Exception as error:  # health: allow BLE001 -- legacy callers require None; log redacted text rather than a secret-bearing exception traceback
        failure = adapter._last_topic_creation_failure = _classify_failure(error)
        if failure.kind == "server_refused":
            # Refused calls can still allocate invisible, unusable topic ids (#944).
            adapter._topic_creation_cooldown_until = time.monotonic() + _COOLDOWN_SECONDS
            logger.warning("[%s] %s", adapter.name, topic_creation_error_message(adapter))
        elif failure.kind == "duplicate":
            # Telegram has no list-topics API; existing topics are mapped from incoming messages.
            logger.info("[%s] DM topic '%s' already exists in chat %s (will be mapped from incoming messages)",
                        adapter.name, name, chat_id)
        elif failure.kind == "threaded_mode_disabled":
            logger.warning(
                "[%s] Cannot create DM topic '%s' in chat %s: Threaded Mode is not enabled. "
                "The bot owner must open the BotFather Mini App (search 'botfather' in "
                "Telegram, tap Open on the search result) -> My bots -> this bot -> Bot "
                "Settings -> Threads Settings -> enable Threaded Mode. This cannot be enabled "
                "from the DM chat, nor from the BotFather /mybots text menu.", adapter.name, name, chat_id)
        else:
            logger.warning("[%s] Failed to create DM topic '%s' in chat %s: %s",
                           adapter.name, name, chat_id, _redact_telegram_error_text(error))
        return None
