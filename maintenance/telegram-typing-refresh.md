# Telegram typing refresh re-arm

Load this unit when changing Telegram typing refresh cadence or the post-send typing re-arm limiter.

## Required behavior

- Telegram refresh requests use the adapter's bounded 4.5s default (1.0–4.75s override); other platforms retain the 2.0s base default.
- The refresh deadline is anchored at tick start, so a slow `sendChatAction` does not extend the configured request spacing.
- Intermediate sends schedule typing re-arms off the send path. The per-chat limiter (default 2s, configurable via `typing_retrigger_min_interval_seconds`; 0 disables it) emits one immediate re-arm and defers one trailing re-arm for requests inside the window; later requests coalesce into it.
- Final replies, turn cleanup, and `pause_typing_for_chat` cancel any pending trailing re-arm, and a trailing re-arm that wakes while the chat is paused does not send. The pause path covers edit-only finalization, which never reaches the notify send. There is never more than one re-arm request in flight for a chat.

## Provenance

- Patch identity: `telegram-typing-refresh-4500ms`.
- Source surfaces: `gateway/platforms/base.py` and `plugins/platforms/telegram/adapter.py`.
- Regression: `tests/gateway/test_typing_refresh_interval.py` and `tests/gateway/test_telegram_typing_retrigger.py`.
- Approved scope: Telegram only; daily message budget (1,500), 10:00 UTC anchor, and background limits remain unchanged.

## Retirement

Remove this unit when released upstream provides an equivalent adapter-specific typing cadence and deferred, coalescing post-send re-arm with final-turn cancellation.
