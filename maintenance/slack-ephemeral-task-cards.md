# Slack ephemeral task cards

Load when changing Slack native task-card (plan-mode progress) start, reopen, stop, or cleanup.

## Required behavior

The native "Hermes is working" card is transient progress. While a turn runs it streams as today. When the turn ends for any reason (completed, failed, `/stop`, superseded), the adapter seals the stream with `chat.stopStream`, then removes every card message it opened for that turn with `chat.delete`, including a card replaced after Slack expired its stream. Only the real reply remains. Sealing comes first so a failed deletion never leaves Slack's live working indicator armed. Cleanup failures are logged and never block reply delivery. `message_not_found` is tolerated.

Every successful start, including a replacement after stream expiry, atomically records its workspace, channel, timestamp, and creation time in `get_hermes_home()/state/slack_task_cards.json`. Confirmed deletion and `message_not_found` remove that entry. Other failures retain it indefinitely, including failures older than 24h. An adapter-owned background worker retries every 45 seconds with a 5-second timeout per Slack cleanup call. Cleanup errors remain logged and cannot raise into reply delivery.

After workspace authentication, connect schedules a background sweep of recorded cards not owned by a live stream in this process. Each sweep best-effort seals the recorded card before deleting it, uses its workspace's client, and leaves active cards and unavailable workspaces alone. It never scans Slack history or deletes unrecorded messages. Disconnect cancels the worker before closing workspace clients. Disabling ephemeral deletion prevents both ledger writes and sweeps, including against existing entries.

`chat.postEphemeral` is not used: ephemeral messages cannot be streamed or updated, so they cannot carry live progress.

`platforms.slack.extra.native_task_cards_ephemeral: false` restores the sealed, persistent card. The default is on.

## Provenance and adoption

- Fork patch identity: `slack-ephemeral-task-cards`.
- Owner: `plugins/platforms/slack/task_cards.py` (`TaskCardCleanup`, `finish_task_card`) and the native task-card stream bookkeeping in `plugins/platforms/slack/adapter.py` (`_NativeTaskCardStream.message_timestamps`).
- The turn runner already calls `stop_native_task_card_progress` from its `finally` path, so every turn outcome reaches cleanup. Telegram and the relay connector are unchanged.

## Verification and retirement

The behavior contract is `tests/plugins/platforms/slack/test_task_card_cleanup.py`, with the native task-card cases in `tests/gateway/test_slack.py` and `tests/gateway/test_task_card_destination_preflight.py`.

Retire this patch only when upstream offers equivalent end-of-turn removal and durable restart/retry recovery. Revert the owned files to roll back, or set the config key to `false` without a code change.
