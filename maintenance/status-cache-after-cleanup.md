# Status bubble cache after cleanup

## Contract and placement

`send_or_update_status` on Telegram and Slack remembers one bubble per
`(chat, status_key)` so repeat statuses edit in place (#30045). End-of-turn
progress cleanup (`display.platforms.<p>.cleanup_progress`) deletes every
status bubble the turn tracked, through the adapter's `delete_message`. A
successful delete must also drop the matching cache entry, so the next turn's
first status posts fresh instead of editing a message the platform no longer
has. The fix lives in each adapter's `delete_message`, the one place that knows
the message is gone. It adds no config or plugin surface.

Fork patch identity: `status-cache-after-cleanup`.

## Evidence and provenance

With Telegram `cleanup_progress: true`, the memory recall indicator
(`agent/turn_context.py`, emitted via `_emit_status` as a `lifecycle` status)
is sent on most turns and deleted at the end of each. On 2026-09-24,
`errors.log` held 81 `Failed to edit Telegram message N: Message to edit not
found` ERRORs in about 12 hours since the last restart, preceded each time by a
MarkdownV2 fallback warning. There were 372, 444 and 572 on Sep 21, 22 and 23.
Each fired about one second after an inbound message, before the turn's first
model call, when the recall indicator tried to edit the previous turn's deleted
bubble. The adapter then sent fresh, so users saw no missing message. The cost
was two failed API calls and an ERROR line per turn, which buried real errors.

Upstream `main` has the same cache and the same cleanup path; this is not
fork-induced.

## Verification

`tests/gateway/test_telegram_status_update.py` and
`tests/gateway/test_slack_status_update.py` each hold a regression that sends a
status, deletes it, and sends the next status. They fail without the patch
(an edit of the deleted id) and pass with it.

## Retirement

Retire when upstream drops deleted ids from the status cache, or when cleanup
stops deleting status bubbles. Then remove the two adapter hunks, the tests if
upstream carries equivalents, this unit and its `MAINTENANCE.md` row.
