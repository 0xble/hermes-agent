# Missed inbound recovery

**Patch identity:** `missed-inbound-recovery`. Earlier branch commits of the same unit carry `Bounded restart recovery`.

The branch history also holds an untrailered revert of the first late-dispatch commit. This
stable patch ID classifies only that content.

Fork-Patch-Backfill: a9b87e39012c9d7438581897577998811f523a63; missed-inbound-recovery

Load this unit when changing `gateway/missed_inbound_recovery.py`, the `missed_inbound:*` rows
in `state_meta` (`hermes_state_missed_inbound.py`), the Slack scan
(`plugins/platforms/slack/adapter_missed_inbound.py`), or these Slack adapter points:
`_prefilter_inbound`, the accept point and the `handle_message` call in `_handle_slack_message`,
and `_restart_socket_mode`.

## Required behavior

- A message a person sent while a platform was disconnected is ANSWERED late. It is handed to
  the adapter's own inbound handler, the one the live transport calls, so authorization, mention
  rules, channel gating, session routing and threading are decided by the live code. Recovery
  never posts an acknowledgement in place of an answer.
- The agent reads a note in front of the message: `[Delivered late: sent <local time>, the
  gateway was offline from <start> to <end>. Acknowledge the delay, answer what still applies,
  and confirm before acting on anything time-sensitive.]`. A late `/command` is not executed. It
  reaches the agent as text.
- Unauthorized users are never answered: the scan drops them, and the live handler rejects them
  again on dispatch.
- A message is answered at most once. Its claim (`missed_inbound:claim:<platform>:<conversation>:<ts>`)
  is written before dispatch and is never released, so a restart or a crash loop cannot answer it
  again. A failed dispatch keeps its claim: answering twice is worse than missing one message.
- A message that arrived live is claimed too (kind `live`), so a later scan never replays it. A
  platform replay of a message that was answered late is dropped on the live path, also in a
  process that started after the recovery.
- The window is the offline interval: from the per-platform watermark
  (`missed_inbound:watermark:<platform>`, the last moment the platform was known connected) to
  the reconnect, capped at 24 hours. It is never the previous run's uptime. A platform without a
  watermark has never been connected, so its first connection fetches nothing.
- Messages are dispatched oldest first. At most 20 are dispatched per scan. The rest are claimed
  and get 1 summary line per conversation.
- A reconnect scans only the platform that reconnected.
- Recovery never raises into startup or reconnect. A failed scan logs a warning.
- `gateway.missed_inbound_recovery.enabled` defaults to on for a real gateway and off under a
  test run (`hermes_state_guard._in_test_context`). There is no other setting.

## Watermark

A heartbeat moves each connected platform's watermark every 30 seconds. An adapter can report
its transport through `missed_inbound_live_until()`: a timestamp advances the watermark to that
moment, `False` means the transport is down, `None` means unknown and holds the watermark. A
transport seen down and then up again is scanned from the held watermark. A scan in flight owns
the watermark, so the heartbeat cannot move it past an interval that is still being read.

Slack reports the last Socket Mode ping/pong. A wedged socket therefore stops the watermark at
the last proven moment instead of at the last heartbeat.

## Adapter status

| Adapter | Backlog while disconnected | Status |
|---|---|---|
| Slack | Not queued by Slack for a disconnected Socket Mode app | Recovered here: 1:1 DMs, @-mentions in group DMs and member channels, replies in threads the bot has a session for |
| Telegram | Queued by Telegram and redelivered by `getUpdates` on the next poll | No change needed. Telegram does not implement `fetch_missed_inbound` |
| Others | Not surveyed | Not recovered. An adapter opts in by implementing `fetch_missed_inbound` and `dispatch_missed_inbound` |

## Slack scan

`users.conversations` is read with cursor pagination. 1:1 and group DMs are read first, then
known threads, then channels. Each conversation costs 1 `conversations.history` page for the
window. A thread costs 1 `conversations.replies` page with `oldest` set to the window start.
Known threads are the Slack sessions active in the last 7 days, at most 40. The scan stops at a
call budget of 100 and at the first rate limit, and keeps what it found.

A message is skipped when the bot already posted after it, in the same thread, or in the same
conversation when the bot answers flat (`reply_in_thread: false`).

## Known limits

- A scan that fails, is rate limited, or runs out of call budget is not retried. Conversations
  it did not read stay unanswered for that interval.
- Socket Mode's internal reconnects, where `slack_sdk` replaces a session without the adapter
  rebuilding the handler, are not hooked. They are covered only when the heartbeat sees the
  transport down between two ticks. An adapter-level `_restart_socket_mode` does trigger a scan.
- A conversation with more than 200 messages in the window is read for its newest 200.

Contract tests: `tests/gateway/test_missed_message_ack.py`, `tests/plugins/platforms/slack/test_missed_message_ack.py`.
