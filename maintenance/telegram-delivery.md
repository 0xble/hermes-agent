# Telegram delivery: flood recovery, split sends, and emphasis

Load this unit when changing Telegram send, edit, or typing paths, the delivery ledger,
flood handling, or legacy-emphasis rendering. Rich rendering modes and paragraph spacing
are owned by [Telegram rendering](telegram-rendering.md).

## Required behavior

- One known flood window per chat is shared across text, edits, typing, uploads,
  rich/plain drafts, control prompts and deletions. Draft/control/delete calls recheck
  the window under the existing per-chat send lock. A new RetryAfter from those paths
  arms the same window, including timedelta values. Rich drafts do not fall back to a
  plain draft after a flood refusal. Draft/control results retain `flood_control` and
  `retry_after`; deletion returns false and retains cached status ownership for retry.
  These are reactive guards, not a durable deletion queue. Media rate limits are
  classified, but attachments are not durable redelivery obligations.
- The full server `retry_after` is enforced for rich sends and edits, typing, and
  the other guarded routes across adapter replacement and gateway restart.
- Split sends resume after flood refusals; rejected deliveries back off and preserve
  recovery; a partial delivery does not trigger a duplicate fallback.
- Nested and multiline legacy emphasis is preserved.
- Synthetic gateway event IDs may identify notifications but never become Telegram reply
  anchors. Numeric Telegram message IDs still anchor ordinary DM-topic replies.

## 2026-09-28 rich egress flood incident: independent diagnosis

At 21:30 PDT the live Telegram API returned a roughly five-hour `RetryAfter` while inbound
events and response generation continued. The rich final-send route returns a transient
`SendResult` carrying that wait but never arms the adapter's shared per-chat flood window.
Repeated rich sends therefore make more API calls during the known penalty. The existing
window also caps enforcement at 300 seconds and exists only in memory, so other routes
would resume too early or immediately after a gateway restart. The exact request that
first crossed Telegram's quota is not established by the logs.

**Patch boundary:** A structured flood refusal from rich send, rich edit, typing,
legacy final edit, or overflow continuation must arm the same per-chat window as
legacy sends, drafts, and media. No formatting fallback may spend another request
inside a known penalty. The guard must enforce
the complete server deadline across adapter replacement and gateway restart. A refusal
must return `flood_control:<remaining seconds>` so the delivery ledger defers final
reply recovery. Read timeouts and ambiguous network failures retain the no-resend rule.
An accepted rich send must never trigger a legacy fallback.

**Alternatives:** Reducing progress output lowers load but cannot enforce a published
deadline. Falling back to MarkdownV2 immediately after a 429 spends another request
inside the penalty. Keeping only an in-memory cooldown still loses the deadline on
restart. A new generic retry framework is unnecessary for this adapter-owned contract.
The narrow solution is a durable, profile-scoped deadline and reuse of the existing
fail-closed result across every guarded egress path.

**Upstream comparison (2026-09-28):** [#46762](https://github.com/NousResearch/hermes-agent/issues/46762)
reported the rich-send retry gap and is closed. [PR #47307](https://github.com/NousResearch/hermes-agent/pull/47307)
is open and proposes immediate Markdown fallback after a rich 429. That addresses a
rendering symptom but conflicts with the server's multi-hour wait in this incident.
[#107612](https://github.com/NousResearch/hermes-agent/issues/107612) tracks the wider
outbound budget gap and remains open. No released equivalent to this full-deadline,
restart-surviving guard was found in the inspected current fork/upstream paths.

**Regression:** A rich 429 with a long wait must prevent another rich or plain send,
including from a newly constructed adapter using the same profile; an unrelated chat
must remain usable, and a completed deadline must expire. Failed rich edits must
enforce the same window. No new Bot API calls are part of the correction. A small
profile-local deadline record is additive; rollback can ignore it. Platform scope is
Telegram on every Hermes host. Whether Telegram applies a given penalty to one chat or
the whole bot remains uncertain, so this patch follows the existing per-chat contract.

**Patch identity:** `telegram-retry-after-egress`. Source surfaces:
`plugins/platforms/telegram/adapter.py` and `flood_state.py`; proof surface:
`tests/gateway/test_telegram_flood_coherence.py` and the adjacent split-send/rich
tests. SQLite stores the deadline in the profile; an atomic per-chat file preserves
it if a SQLite write fails. A storage read error refuses requests in 60-second
increments until the store can be read. If all profile writes fail, only the
current process retains the deadline and durability is degraded. Retire when an
upstream release enforces full rich and typing RetryAfter
deadlines across restart without duplicate sends. Roll back this patch's adapter
classification, persistence calls and `flood_state.py` together, leaving the
earlier flood-coherence routes intact; the additive deadline DB can remain unused.

## Rate Boundary

**Patch identity:** `telegram-chat-budget` (re-expresses archived-fork HERMES-137, HERMES-084,
the HERMES-004 coverage gaps and HERMES-005's producer rule on this fork's adapter).

**Incident (2026-10-04).** The personal DM took a 5464s ban at 01:10 and a 24237s ban at
20:14:48 (an `editMessageText` refused through `flood_guard`, which recorded the deadline
without logging it). The chat carried ~28-56 concurrent topic sessions a day, double the
prior days. The old limiters were a 1.0s send+interim-edit slot (60/min, the whole private
ceiling by itself) beside one 2.0s typing loop per active session (30/min each, skipped only
while a send held the lock), with final edits, drafts, deletions, topic edits and reactions
unmetered. With 5-10 concurrent turns the typing loops alone could reach 150-300/min
(estimate from turn durations: successes log only at debug).

**Independent hypothesis (frozen before upstream search).** Every Bot API call to a chat
spends one Telegram allowance, but the fork metered a subset with a limiter sized at the full
ceiling. Fan-out multiplies the unmetered per-session typing loops. The correction belongs at
the one point every request crosses, PTB's request layer (`ExtBot._do_post` passes every
endpoint except `getUpdates`, raw `do_api_request` included, through `rate_limiter`), with one
per-chat clock sized below the class ceiling and cosmetic traffic shed rather than queued.
Weaker alternatives: per-path throttles (the sum stays unbounded, the original defect); PTB's
`AIORateLimiter` (queues instead of shedding, needs `aiolimiter`, and has no notion of
superseded previews or the durable deadline); config changes such as disabling typing or
progress (degrade the product without bounding the sum).

**Upstream (2026-10-04).** #107612 (open) tracks the missing shared budget. #99643 (open) and
its unreviewed PR #99676 cover only typing and keep a `min()` floor that collapses to 1s.
#107133 (open) is same-chat session fan-out, mitigated here but not fixed: N concurrent finals
still share one chat's budget. All-state PR searches for `rate limiter telegram` and
`per-chat budget telegram` found no implementation. Fork PR #304 (open) adds goal-notice
retries after short flood windows. Those retries now spend the same slot, so it cannot exceed
the budget. Checked and already native, so not ported: HERMES-085 (`delivery_ledger.py`
and `outbox.py` treat `flood_control:<s>` as a timed deferral) and HERMES-109 (merged as #152,
see `telegram-internal-delivery-recovery.md`).

**Contract.** `plugins/platforms/telegram/chat_budget.py` owns one clock per chat.
`ChatBudgetRateLimiter` is installed on the gateway Application, and `MeteredBot` wraps the
standalone sender's bot. Each metered request (any non-`get*` endpoint carrying `chat_id`) takes
the chat's next slot. Deliveries (sends, final and over-cap edits, overflow continuations,
media, controls, deletions, topic edits, reactions) wait FIFO and are never dropped. Typing and
drafts are shed when no slot is free. Interim edits are skipped by the adapter inside the
10.0s edit floor. The adapter pre-waits sends and final edits under the chat lock so pacing
never eats a transport deadline. A request inside a durably recorded server penalty is refused
locally with `RetryAfter` for every path. Any published `retry_after` widens that chat's gap
2x for 10 minutes from the next call, and is persisted and logged. The inline-wait cap is
floored at the chat's gap. Bubble cleanup uses `deleteMessages` (100 ids per request).
`TelegramAdapter.PROGRESS_EDIT_INTERVAL` is 10.0s, the transport edit floor, and
`TurnRunner._progress_edit_interval` uses it for Telegram progress bubbles. Other platforms keep
the runner default (`TurnRunner._PROGRESS_EDIT_INTERVAL`, 3.0s). Telegram used 3.0s until
2026-10-06, when the daily call counter showed progress-bubble edits were the largest share of
typed-turn calls on a chat that hit a daily volume ban; Brian approved slower bubble updates.

| Path | Private worst case | Group worst case |
| --- | --- | --- |
| All metered calls to one chat (one shared slot) | 45/min (1.33s gap) | 15/min (4.0s gap) |
| of which typing, at most | 15/min (4.0s) | 5/min (12.0s) |
| of which interim edits and drafts, at most | 6/min (10.0s) | 6/min (10.0s) |
| After a published `retry_after` (10 min) | 22.5/min | 7.5/min |
| Ceiling (community envelope) | ~60/min | ~20/min |

The sum is bounded by construction, because every path takes the same slot: 75% of each class
ceiling regardless of concurrent sessions or topics. Not shared across processes: the slot
clock (each process meters itself). Shared across processes: the durable penalty deadline,
which the standalone lane now honours, so cron's standalone fallback can no longer spend
requests or sleep for hours inside a ban. Reads (`get*`) and chat-less calls such as
`answerCallbackQuery` are unmetered. Visible effect: with many topics active at once, typing
indicators refresh chat-wide at most every 4s, so not every topic shows "typing" continuously,
and progress bubbles update at most every 10s.

**Daily volume shedding** (`daily_quota.py`, 2026-10-07). Per-chat message-creating calls are
counted per Telegram day against `daily_message_soft_ceiling` (default 1500). Turns not typed by
the user shed typing, interim edits, drafts and progress at 70% of it, and non-final notices at
100%. Finals are never shed. Cleanup deletes are never shed either: they create no message, and
shedding them stranded progress bubbles whenever a typed turn's cleanup ran after an in-band drain
had relabelled the task as a background trigger (2026-10-08 to 10-10, ~170 failures/day). The
Telegram adapter now runs post-delivery callbacks under the trigger of the turn that registered
them. Regression: `tests/gateway/test_telegram_daily_quota.py`.

**User voice echoes** (`telegram-chat-budget`). The shared gateway STT echo path
classifies a response to a non-internal voice event as `OUTBOUND_FINAL` for budget
priority, regardless of the queued task's inherited trigger or outbound class.
The `_interim_send` marker remains intact for stream-is-the-message adapters;
quota priority does not seal the running stream. Internal producers retain their
existing classification. Failed `SendResult`s (including `daily_budget_shed`) and
exceptions are WARNING-logged without adding transcript text to the log.
Regression: the same daily-quota suite drives busy FIFO prefetch, overflow drain,
and idle enrichment through the real Telegram send with goal/untagged pressure,
checks topic routing and interim metadata, and verifies background traffic still
sheds. Roll back by reverting the gateway echo/event binding and these tests;
retire when an accepted upstream release protects originating-user echoes under
an equivalent quota contract. No configuration or persistent-state changes.

**Regression:** `scripts/run_tests.sh tests/gateway/test_telegram_chat_outbound_budget.py`
pins the summed per-chat rate against each class ceiling with every path saturated at once,
classification by id, widening on the real error path plus its scope and expiry, the
long-penalty refusal for every endpoint, the inline-cap floor, concurrent deliveries sharing
the slot, the edit floor and metered finals, the producer interval against the imported floor,
batched cleanup and its flood retention, and the standalone lane. The 30-session typing case
fails on the pre-patch base (30 chat actions instead of 1).

**Rollback:** Revert the `fix(telegram): one outbound budget per chat` commit. It removes
`chat_budget.py`, the builder `rate_limiter`, `MeteredBot` in `tools/send_message_senders.py`,
`delete_messages` (base, Telegram, cleanup grouping), the inline-cap helper and the 3.0s
producer interval, and restores the 1.0s slot helpers. No state, schema or configuration
migration is involved.

**Retirement:** Retire when released upstream meters one per-chat budget across every Bot API
call, sized by chat class, shedding cosmetic traffic and widening on `retry_after`, and passes
the regression above.

## Deferred Outbound-Class Isolation

**Patch identity:** `telegram-outbound-class`. Durable outbox rows preserve their outbound class
before a deferred sweep is created, and replay binds that class explicitly. A sweep created while a
notice or progress send is in flight therefore cannot shed a replayed final at the daily ceiling;
legacy rows without a class default to the protected final class, while interim metadata remains
progress. Source: `gateway/outbox.py`; proof: `tests/gateway/test_outbox_coalesced_sweep.py`.
Retire when the durable delivery owner records and restores outbound classes through an equivalent
released upstream mechanism.

## Provenance and patches

- Fork patch identities: `slice-10-flood-coherence`, `slice-10-telegram-delivery`,
  `slice-10-telegram-delivery-followup`, `slice-10-delivery-ledger`,
  `slice-11-telegram-emphasis`, `telegram-delivery` (synthetic reply-anchor guard).
- Adopted upstream commits: split-send recovery `595f3a289c7`, partial-delivery suppression
  `969898d4ff4`, redelivery backoff `c961e5bb691` and `807435ac1ec`. All four are
  ancestors of upstream release v2026.9.24 (`f97608f178d1ffeca59860195ab7da295f7c8e5f`),
  verified on 2026-09-26. They are released native behavior, not pending borrowed
  changes. Separate local flood-coherence and ledger deltas remain. Flood coherence
  is tracked in [issue #107612](https://github.com/NousResearch/hermes-agent/issues/107612).
- Emphasis is an own contribution: [upstream PR 106906](https://github.com/NousResearch/hermes-agent/pull/106906),
  open at `37f872bad1706c6c50ccdccb825fc4d5ffd2c246` on 2026-09-19.

## Reconnect Teardown During Text Sends

**Patch identity:** `telegram-send-teardown`. A text send can pass admission,
then wait for its chat lock, pacing slot, or retry while disconnect fences the
adapter and clears its bot. Recheck before starting each Markdown/plain request
and after lock/pacing waits. Return the existing retryable, pre-send refusal so
the delivery owner keeps the reply. Preserve delivered split chunks and their
certain remainder. If a previous transport request had an uncertain outcome,
preserve its error instead of converting it into a certain pre-send refusal.

Source: `plugins/platforms/telegram/adapter.py`. Proof:
`tests/gateway/test_telegram_send_teardown.py`, plus reconnect, split-send and
send-path health tests. No new calls, retries, shorter pacing, or longer turns
are introduced. The inherited shared-rate limitations in Rate Boundary remain
unresolved. Retire after an accepted upstream release passes these behavioral
tests without this patch. Roll back this patch's adapter and tests together.
There are no configuration or persistent-state changes.

## Standalone Chunk Indicators

**Patch identity:** `telegram-standalone-chunk-indicator`. When cron delivery
falls back from the live adapter to the standalone sender (flood control,
timeout, `send_path_degraded`), a long message is split and each chunk ends with
a ` (n/m)` indicator. Those parentheses are reserved in MarkdownV2, so Telegram
rejected every chunk with `Can't parse entities` and each one arrived as plain
text. The live adapter already escapes the indicator, but the standalone sender
did not. On 2026-10-01 standalone fallbacks rose from about 2 a day to 41, which
made the problem visible across many crons.

The adopted fix is upstream salvage PR
[#126100](https://github.com/NousResearch/hermes-agent/pull/126100), for issue
[#74004](https://github.com/NousResearch/hermes-agent/issues/74004). It is
cherry-picked with original authorship. It escapes the indicator and separates
it from a closing code fence, reusing `_separate_chunk_indicator_from_fence`.
Source: `tools/send_message_senders.py`. Proof:
`tests/tools/test_telegram_send_message_chunk_mdv2.py`, which fails on the
unpatched sender. The standalone lane still sends MarkdownV2, never Rich
Messages. That gap is unchanged.

Retire after an accepted upstream release contains #126100, or an equivalent,
and the proof test passes without this patch. Roll back by reverting the two
commits. There are no configuration or persistent-state changes.

## Cron Short Flood Wait

**Patch identity:** `cron-short-flood-wait`. The standalone lane sends legacy
MarkdownV2 only, so a cron that falls back there loses Rich Message features:
`[^n]` footnotes arrive as literal text, and tables and `<details>` flatten. On
2026-10-03 a personal-alerts delivery fell back because the live adapter refused
it locally with `flood_control:3.59` while four alert monitors and active chats
shared one DM. The standalone sender then sent 0.8s later, inside the window.

The live lane now sits out a `flood_control:<seconds>` refusal and retries on the
live adapter, as long as the cumulative wait for that target stays within
`_LIVE_FLOOD_WAIT_BUDGET_SECS` (15s). Every other error still falls back to
standalone. A flood refusal past that budget fails closed instead (see
[Cron Flood Fail-Closed](#cron-flood-fail-closed)). Source:
`cron/scheduler_delivery.py` (`_short_flood_wait`, `_live_send_text`). Proof:
`TestShortFloodWaitStaysOnTheLiveLane` in
`tests/cron/test_cron_live_delivery_confirmation.py`, which fails without the patch.

Rate budget: no new calls. A refused live attempt during a known window makes
no API call. The retry replaces the standalone send that would otherwise have
followed, and moves it after the published window instead of inside it. The
worker thread blocks for at most 15s per target. Cron output is not
latency-sensitive. No upstream issue or PR covered this on 2026-10-03. Retire
when the standalone lane can send Rich Messages, or upstream retries short live
floods equivalently. Roll back by reverting the commit. There are no
configuration or persistent-state changes.

## Cron Flood Fail-Closed

**Patch identity:** `cron-flood-fail-closed`. Once a Telegram live send is refused with an
active flood-control deadline that the short wait above cannot sit out, cron delivery
records the target as deferred and does not enter the standalone sender. A second
sender during the same penalty can extend or obscure the ban, and the standalone lane
also drops Rich Message features. Relay targets and non-Telegram platforms are
unchanged, and non-flood errors still fall back to standalone. Source:
`cron/scheduler_delivery.py` (`_live_flood_held`, `_deliver_standalone`,
`_warn_live_lane_failure`). Proof: `test_long_flood_fails_closed_without_standalone`
and `test_repeated_floods_stop_at_the_budget` in
`tests/cron/test_cron_live_delivery_confirmation.py`.

Landed as [#335](https://github.com/0xble/hermes-agent/pull/335), commit
`90fdfcecf4f5`, without a `Fork-Patch` trailer. The backfill line below records its
stable patch ID, so `main` keeps passing the trailer check without rewriting
published history.

Fork-Patch-Backfill: 3b2042b08305abd280088b719767fdec1cc3ed92; cron-flood-fail-closed

Retire when upstream cron delivery stops falling back to a second sender during an
active Telegram flood deadline. Roll back by reverting `90fdfcecf4f5` and restoring the
`Cron Short Flood Wait` fallback text. There are no configuration or persistent-state
changes.

## Replacement Adapter Egress

**Patch identity:** `telegram-replacement-adapter-egress`. When polling recovery
rebuilds the adapter, a turn already in flight keeps the retired instance, whose
`_bot` is gone. `send()` already forwards to the live adapter in `runner.adapters`.
`edit_message()`, `delete_message()` and `send_typing()` did not. Their refusal was
not retryable, so the progress loop stopped editing and sent every later tool line
as its own reply. Observed 2026-10-03 in the Booking Analytics topic after the
12:33 PDT adapter rebuild. These three calls now forward to the live adapter. With
no live adapter, an edit returns a retryable `Not connected` unless the failure is
permanently fatal.

Source: `plugins/platforms/telegram/adapter.py`. Proof:
`tests/gateway/test_telegram_replacement_adapter_egress.py`, red on the base. No new
request types: a forwarded call replaces one that would otherwise have been a
fresh send. Media sends (`send_image`, `send_voice`, `send_multiple_images`, local
files) still refuse on a retired instance and remain a follow-up. Upstream has the
same gap at `343500b354`. Retire when an upstream release forwards these calls.
Roll back by reverting this patch's adapter and test changes. No state changes.

## Transient Rich Delivery Recovery and Capability Latch

**Patch identity:** `telegram-rich-delivery-recovery`. Cron delivery keeps a Telegram
live-adapter send on the Rich Message path for a bounded 120-second exponential-backoff
window after `send_path_degraded` or a short flood refusal. Only after that window does
it use the legacy standalone sender. If that fallback succeeds after a transient live
failure, the job records `last_delivery_formatting_degraded` with the affected target
and emits a WARNING; non-Telegram targets are unchanged. The existing delivery ledger
remains the recovery path when fallback cannot send.

Rich capability rejection is WARNING-logged with the existing redaction helper and the
adapter latch resets at the next polling generation. The latch still suppresses retries
within one generation, so a genuine unsupported endpoint cannot create a retry storm.
The current fork already contains the currency protection from `f9a4ab8558`; a direct
payload reproduction for `costs $500 and $1,200` produces ``costs `$500` and `$1,200` ``
and does not reproduce the reported LaTeX defect, so no currency source change is made.

Source: `cron/scheduler_delivery.py` and `plugins/platforms/telegram/adapter.py`.
Proof: `tests/cron/test_cron_reconnect_only_rejection.py` and
`tests/gateway/test_telegram_rich_messages.py`. Upstream search on 2026-10-04 found no
matching issue or pull request for these exact symbols. Retire when an upstream release
keeps transient cron delivery on the rich live lane and resets capability latches by
polling generation. Roll back the two source files and their regression tests together;
there are no configuration or persistent-state migrations.

## Delivery Verification

`scripts/run_tests.sh` on `tests/gateway/test_telegram_flood_coherence.py`,
`tests/gateway/test_telegram_split_send_flood.py`,
`tests/gateway/test_delivery_flood_invariants.py`, the `test_delivery_ledger*.py` files,
and `tests/gateway/test_telegram_emphasis.py`. The synthetic anchor regression is
`tests/gateway/test_telegram_thread_fallback.py`; boot auto-resume injection and
numeric normal replies must both reach the Telegram fake transport.

## Retirement and rollback

The four adopted commits are already contained in v2026.9.24. Retire only their
redundant adaptation after checking the selected release against the regressions,
while preserving the independently required local deltas. Retire flood
coherence when upstream classifies media floods and shares one per-chat window. Retire
emphasis when PR 106906 merges and the candidate tag includes it. Roll back by reverting
the logical patch; no persistent data changes.

## Daily call counter (measurement only)

**Evidence (2026-10-06).** The last two bans were each a single 429 with a multi-hour wait:
`editMessageText` at 2026-10-04 20:35 (23019s) and at 2026-10-05 17:23:52 PDT
(`retry_after=34507.0s`, the first 429 that day). Both ended near 02:59 PDT (09:59 UTC). In the
24 minutes before the second one the log shows only progress edits, a few `deleteMessages`
bubble cleanups and sends, about 1 call every 4-6s, inside the 45/min budget above. So the
per-minute budget cannot be the binding limit. The fixed end time points to a volume window.
Its size, and whether it applies per chat or per bot, were not known because successful calls
were only logged at debug level.

**Contract.** `ChatBudgetRateLimiter` counts every metered call that reaches Telegram in
`DailyCallCounter`, by chat, endpoint and trigger, in hourly buckets, after the request returns so
measurement never shifts pacing. Local penalty refusals and shed typing or drafts are not counted.
`TelegramAdapter._process_message_background` binds the trigger (`typed`, `goal`, `loop`, `relay`,
`process`, `delegation`, `restart`, `heartbeat`, `internal`) in a ContextVar for the turn task, and
`get_pending_message` rebinds it when the runner drains a queued event in-band, so a turn queued
behind a busy session is never counted under its predecessor. Calls outside a turn (cron delivery, outbox
replay, housekeeping) are `untagged`. Counts flush additively to `call_counts` in the profile's
`telegram-flood-state.db` at most once a minute, are kept for 30 days, and log a 24h summary hourly
(whole hourly buckets from the first hour at or after the cutoff, never reaching back before it).
Each counter starts one long-lived daemon worker when it is created, so a quiet profile persists
within a minute and no send pays for a thread start. Window reads include counts a locked database
could not store yet. Any `retry_after` of 600s or more also logs every chat's window counts for the profile, so per-chat and per-bot limits can be told apart. Each profile directory gets its own counter, resolved on the caller's context, never on the worker thread. The send path
only updates an in-memory dict. Persistence and summaries run on the counter's own daemon thread,
so a slow or locked database cannot delay a call. The counter never sheds or refuses a call.
Counter failures are logged at debug level and unflushed counts are kept for the next flush.

**Reading it.** `sqlite3 ~/.hermes/telegram-flood-state.db "select chat_id, endpoint, trigger,
sum(count) from call_counts where hour >= strftime('%s','now','-1 day') group by 1,2,3"`. The
threshold is the window total logged with the next long `retry_after`. Comparing per-chat
totals across chats at that moment shows whether the limit is per chat or per bot.

**Regression:** `scripts/run_tests.sh tests/gateway/test_telegram_daily_call_counter.py`.

**Rollback:** Revert the `feat(telegram): count daily calls per chat` commit. The
`call_counts` table can stay, because nothing else reads it.

**Retirement:** Retire once the threshold is measured and upstream exposes equivalent per-chat
call accounting, or once the early-warning cron reads another source.
