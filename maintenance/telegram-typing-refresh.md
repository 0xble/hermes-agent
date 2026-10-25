# Telegram typing refresh re-arm

Load this unit when changing Telegram typing refresh cadence or the post-send typing re-arm limiter.

## Required behavior

- Telegram refresh requests use the adapter's bounded 4.5s default (`typing_refresh_interval_seconds`, 1.0–4.75s); other platforms retain the 2.0s base default.
- The refresh deadline is anchored at tick start, so a slow `sendChatAction` does not extend the configured request spacing.
- A Telegram refresh tick dropped because a delivery holds the per-chat send lock, or because the send timed out or failed, is retried once about 0.5s later, never past the next regular deadline. Budget, flood and cooldown sheds are not retried. Other adapters never retry a tick.
- The per-tick send timeout stays below the refresh interval.
- Intermediate sends schedule typing re-arms off the send path. The limiter is keyed per topic, `(chat_id, thread_id)`, with `thread_id` taken from `thread_id` or `message_thread_id` metadata like `send_typing`. It defaults to 2s and is configurable via `typing_retrigger_min_interval_seconds`. Setting 0 removes the time window, but a re-arm already in flight for the topic still absorbs later requests, so 0 does not restore one call per intermediate send. It emits one immediate re-arm and defers one trailing re-arm for requests inside the window. Later requests coalesce into that trailing re-arm only while it is still waiting. Once its send starts, a new request schedules another trailing re-arm after it. There is never more than one re-arm request in flight per topic.
- Final replies, `_keep_typing` cleanup and `_stop_typing_refresh` cancel only their own topic's re-arms. `pause_typing_for_chat` with thread metadata cancels only that topic's re-arms. Metadata-free callers retain chat-wide pause behavior and cancel every topic's re-arms for that chat. A trailing re-arm that wakes while its topic or chat is paused does not send. The pause path covers edit-only finalization, which never reaches the notify send.

## Provenance

- Patch identity: `telegram-typing-refresh-4500ms`.
- Source surfaces: `gateway/platforms/base.py` and `plugins/platforms/telegram/adapter.py`.
- Regression: `tests/gateway/test_typing_refresh_interval.py` and `tests/gateway/test_telegram_typing_retrigger.py`.
- Approved scope: Telegram only; daily message budget (1,500), 10:00 UTC anchor, and background limits remain unchanged.
- File-size ratchet: this patch intentionally keeps the existing adapter surfaces in place; follow up by moving Telegram typing re-arm code into a topical sibling module when the current PR is not constrained to one adapter file.

## Retirement

Remove this unit when released upstream provides an equivalent adapter-specific typing cadence and deferred, coalescing post-send re-arm with final-turn cancellation.
