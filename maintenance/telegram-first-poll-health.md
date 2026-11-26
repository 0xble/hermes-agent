# Telegram first-poll health

**Patch identity:** `telegram-first-poll-health`.

Load this unit when changing the instrumented getUpdates request
(`_instrument_polling_request`), polling-generation fencing (`_begin_polling_generation`,
`_record_polling_progress`), how `start_polling` is called, or when upgrading
python-telegram-bot.

## Required behavior

- Each new polling generation (cold boot, reconnect, conflict retry, network-error restart)
  proves health from its first getUpdates round trip, without waiting for an idle long poll.
  While the current generation has not recorded progress, its getUpdates requests go out with
  Telegram `timeout=0`. The read timeout drops by the same long-poll allowance PTB added.
  `offset`, `limit` and `allowed_updates` are unchanged.
- Health is still recorded only from a successful (`ok: true`) getUpdates response for the
  current generation. Requests from stale, fenced, torn-down or untagged generations keep the
  normal long poll. Once progress is recorded, every later poll keeps PTB's 10s long poll, so
  idle steady state never busy-loops.
- Updates returned by the fast poll go through PTB's normal path: offsets advance, nothing is
  dropped or duplicated. `drop_pending_updates` still runs in PTB's bootstrap, before the first poll.
- If the PTB request shape is unexpected (no `RequestData`, no numeric positive `timeout`, or
  the private `RequestParameter` import fails), the request is sent unchanged.

## 2026-10-10 restart latency

On the planned update restarts at 00:51 and 01:31 PDT, `Telegram polling confirmed healthy:
getUpdates progressing` appeared about 11s after the new gateway connected. The generation's
first getUpdates was an idle long poll with PTB's default `timeout=10s`
(`Updater.start_polling` passes the same timeout to every `get_updates`), and health is only
observed when that response returns. Every restart therefore spent about 10s with the send path
degraded.

PTB closes over a single timeout for the whole polling loop, so `start_polling` cannot set a
different timeout for only the first poll. The adapter already wraps the dedicated getUpdates
request to observe health, so the change lives in that wrapper: an unproven current generation
rewrites `timeout` to 0 for its request. The rewrite stops as soon as progress is recorded.
A failed fast poll (network error, 409 Conflict) records no progress, so PTB's retry is fast
again. That retry still follows PTB's retry interval, so a failing generation is not a busy loop.

**Regression:** `scripts/run_tests.sh tests/plugins/test_telegram_first_poll_fast_ptb.py`
(real PTB 22.8 `Application`/`Updater`). It covers:
- a cold-boot generation's first poll using `timeout=0` and proving health within 1s, while
  Telegram stays idle;
- a pending backlog update delivered exactly once by that fast poll, with the next poll
  acknowledging its offset;
- the second poll returning to `timeout=10` with PTB's widened read timeout;
- a reconnect generation getting the fast first poll again;
- stale-generation and untagged requests keeping the long poll.

On the base source, the first poll of each generation carried `timeout=10`.

`tests/plugins/test_telegram_polling_progress_ptb.py` now counts only `timeout=0` polls made with no
generation in context as PTB `stop()` cleanup. The cleanup poll runs outside any generation, while
a generation's own fast first poll is tagged.

**Rollback:** Revert the commit carrying `Fork-Patch: telegram-first-poll-health`. It makes no
state, schema or configuration change.

**Retirement:** Retire when upstream proves polling health without waiting out an idle long poll,
or when PTB exposes a per-call first-poll timeout. Review on any PTB 23+ upgrade, because the
rewrite depends on PTB 22.x's private `RequestParameter`.
