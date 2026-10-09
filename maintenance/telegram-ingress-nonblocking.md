# Telegram ingress stays non-blocking

**Patch identity:** `telegram-ingress-nonblocking`.

Load this unit when changing the Telegram update consumer: handlers reached from
`TelegramApplication.process_update`, the busy-session and inline command replies, the
pending-update heartbeat probe, or reconnect token ownership.

## Required behavior

- The task running a platform's serial update consumer is marked (`ingress_consumer_scope`).
  On that task, outbound replies never wait for the chat's outbound budget: inline command
  replies (`_dispatch_inline_reply`), busy acknowledgements and busy replies
  (`_send_busy_reply`), and the media-download retry notice all run as tracked background tasks
  through `spawn_ingress_reply`. Off the consumer, including tasks spawned from it, these
  replies are still awaited.
- The pending-update probe escalates only when Telegram reports a backlog **and** no update
  was dispatched since the previous probe. A backlog seen while dispatch progresses starts a
  new window.
- A reconnect waits up to 25s for this process's previous poller on the token to finish and
  release it, instead of refusing while the old poller is still stopping.

## 2026-10-05 Incident

Telegram polling was force-restarted at 12:13 and 13:26 PDT on runtime `d0251975`. The polling
journal (`gateway-coordinator.db`, `telegram_updates.received_at`) showed that each "stuck"
probe had caught a different update shortly after its fetch:

| Restart | Probe 1 caught | Probe 2 caught | Dispatched between |
| --- | --- | --- | --- |
| 12:13 | update 395 (voice), 1.3s in | update 398 (`/s` to a busy thread), 1.8s in | 396, 397 |
| 13:26 | update 515 (`/queue` to a busy thread), 1.2s in | update 519 (`/queue` to a busy thread), 21.5s in | 516, 517, 518 |

Telegram counts the batch being handled as pending until the next `getUpdates` confirms its
offset, and the controlled poller does not poll again until `update_queue.join()` returns. So
any probe landing while a handler runs sees `pending_update_count >= 1`, and two such probes
90s apart read as a wedge. Every recognized command to a busy session runs
`_dispatch_inline_reply` on the consumer. Its reply waits for the chat's FIFO outbound slot
(`telegram-chat-budget`), so update 519's `/queue` reply held the consumer for more than 20s
behind that chat's queued finals. Each restart then lost about 30s: the reconnect watcher
called `connect()` 150ms before the failed generation's poller finished stopping, got "old
Telegram poller still owns this token", and backed off.

`getFile` and file downloads are not metered by the chat budget (`get*` endpoints are exempt,
and downloads bypass `_do_post`). The 12:13 voice download failure was an independent
`httpx.ReadError` on the sticky IPv4 path.

**Not changed:** finals that fail with `send_path_degraded` during the reconnect gap are already
redelivered by the delivery ledger once polling is confirmed healthy. That happened 2 times at
12:13:53–54 and 3 times at 13:27:16–20. Busy acknowledgements and other non-final notices are
not ledgered and can still be lost in that window.

**Regression:** `scripts/run_tests.sh tests/gateway/test_telegram_ingress_nonblocking.py
tests/plugins/test_telegram_ingress_consumer_ptb.py`. It covers:
- the 13:26 probe shape (backlog with dispatch progress) not escalating, while a backlog
  without progress still escalates;
- command replies and busy replies on the consumer returning before the budget slot opens,
  while off the consumer they are still awaited;
- the consumer role not being inherited by spawned tasks;
- the real `TelegramApplication.process_update` marking and clearing the consumer;
- a reconnect waiting for the old poller to release, while a poller that never stops still
  refuses.

On the base source, the same probe sequence triggered a polling restart, and the inline reply
blocked its caller on the budget wait.

**Rollback:** Revert the `fix(telegram): keep the update consumer off outbound pacing` commit.
It makes no state, schema or configuration change.

**Retirement:** Retire the probe change once upstream probes pending updates against dispatch
progress. Retire the consumer offload once replies on the ingress path no longer share a paced
outbound queue.

## 2026-10-08 Dispatcher-stall diagnostics

When the once-per-stall healthy-but-deaf warning fires, the adapter also emits one bounded
`[Telegram] deaf-dispatcher diagnostics:` warning. It includes the PTB update queue depth, the
concurrency and semaphore state when available, the time since the last dispatched update, and the
await chain of up to five PTB fetcher or update-processing tasks. `Task.get_stack()` stops at the
task's own coroutine, so the adapter follows `cr_await` down to the frame that is actually blocked,
which is usually a handler nested under PTB's fetcher and wrapper coroutines. Frames are rendered as
`func@file.py:line`. Collection is best-effort and cannot alter recovery, user-visible Telegram
behavior or pacing.

**Regression:** `tests/gateway/test_telegram_ingress_delivery_gap.py` covers a handler blocked two
awaits below a PTB-named processing task, which must appear in order in the logged chain. It also
covers bounded output, once-per-stall emission, and diagnostic failure isolation.

**Rollback:** Revert the `fix(telegram): log the dispatcher await chain when ingress goes deaf`
commit. It makes no state, schema or configuration change.

**Retirement:** Retire this once a deaf-dispatcher stall has been attributed to a root cause and
fixed, or upstream ships an equivalent dispatcher diagnostic.
