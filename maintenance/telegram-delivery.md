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
3.0s edit floor. The adapter pre-waits sends and final edits under the chat lock so pacing
never eats a transport deadline. A request inside a durably recorded server penalty is refused
locally with `RetryAfter` for every path. Any published `retry_after` widens that chat's gap
2x for 10 minutes from the next call, and is persisted and logged. The inline-wait cap is
floored at the chat's gap. Bubble cleanup uses `deleteMessages` (100 ids per request).
`TurnRunner._PROGRESS_EDIT_INTERVAL` is 3.0s, the transport edit floor.

| Path | Private worst case | Group worst case |
| --- | --- | --- |
| All metered calls to one chat (one shared slot) | 45/min (1.33s gap) | 15/min (4.0s gap) |
| of which typing, at most | 15/min (4.0s) | 5/min (12.0s) |
| of which interim edits and drafts, at most | 20/min (3.0s) | 15/min (4.0s) |
| After a published `retry_after` (10 min) | 22.5/min | 7.5/min |
| Ceiling (community envelope) | ~60/min | ~20/min |

The sum is bounded by construction, because every path takes the same slot: 75% of each class
ceiling regardless of concurrent sessions or topics. Not shared across processes: the slot
clock (each process meters itself). Shared across processes: the durable penalty deadline,
which the standalone lane now honours, so cron's standalone fallback can no longer spend
requests or sleep for hours inside a ban. Reads (`get*`) and chat-less calls such as
`answerCallbackQuery` are unmetered. Visible effect: with many topics active at once, typing
indicators refresh chat-wide at most every 4s, so not every topic shows "typing" continuously,
and progress bubbles update at most every 3s.

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

## Daily Volume Ledger

**Patch identity:** `telegram-daily-volume`. Measurement only: nothing is shed or delayed.

**Why.** The per-chat rate budget (`telegram-chat-budget`) did not prevent the 2026-10-05 ban.
At 17:23:52 PDT the DM was refused for 34507s while traffic was 0-6 sends/min. Every long ban in
`gateway.error.log` since 2026-09-28 has ended at a fixed time of day:

| Refused at (PDT) | retry_after | Ends (UTC) |
| --- | --- | --- |
| 2026-09-28 00:33 | 7202s | 09-28 09:33:24 |
| 2026-09-28 21:30 | 18198s | 09-29 09:33:23 |
| 2026-10-04 01:10 | 5485s | 10-04 09:41:23 |
| 2026-10-04 20:14 | 24231s | 10-05 09:58:45 |
| 2026-10-05 17:23 | 34507s | 10-06 09:58:59 |

The outbox recorded 1929 in-turn sends between the previous reset and the refusal in both of the
last two windows (17.6h and 14.4h). Full days with 498-1103 sends were not banned. That points to a
daily volume cap that Telegram does not document, rather than a rate limit. The outbox misses
out-of-turn sends, edits, typing and deletes, so the threshold is not yet known.
A bot-scoped `setMyCommands` succeeded during the ban, so the refusal is not a bot-wide write
freeze. Whether the cap is keyed per chat or per bot is untested.

**Contract.** `plugins/platforms/telegram/daily_volume.py` counts every metered request, by
endpoint and messages created, per chat and per rolling 24h window. It runs in the gateway's
request-layer limiter and in the standalone sender.
- The window anchors at UTC midnight. A published `retry_after` of an hour or more re-anchors it
  at the moment the penalty ends, carrying the current counts into that window.
- Counts persist in `telegram-flood-state.db` (`daily_volume`, `daily_anchor`, 14-day retention).
  The gateway flushes every 60s and reads the shared totals back. The standalone lane writes per call.
- INFO logs the window's totals hourly. WARNING logs them with every `retry_after`, so the next
  refusal is directly comparable to the volume that preceded it.

Shedding against a daily ceiling was built and parked on `park/telegram-daily-volume-shedding`
(`f8ff9996`). Decide on it after this ledger has recorded at least one refusal.

**Regression:** `scripts/run_tests.sh tests/gateway/test_telegram_daily_volume.py`.

**Rollback:** Revert `fix(telegram): count daily Bot API volume per chat`. The two tables can
remain unused.

**Retirement:** Retire when Telegram documents the cap, or when a shedding policy replaces the
measurement.

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
`_LIVE_FLOOD_WAIT_BUDGET_SECS` (15s). Longer penalties, repeated refusals past the
budget, and every other error still fall back to standalone, as before. Source:
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
