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

The proactive text/interim-edit slot remains 1 second (up to 60 calls/minute).
The base typing loop has a separate default 2-second interval (up to 30/minute
per active loop), and final/overflow edits, drafts, media, control, deletion and
other direct Bot API paths are not all charged to one proactive slot. The sum is
therefore not bounded by this implementation, even before concurrent topic turns.
The 2026-09-27 follow-up adds no requests, retries, or producer frequency. It
closes known-window bypasses and removes duplicate helper definitions. It does
not claim a universal Telegram quota or a complete per-chat proactive budget.
Do not retire the broader scheduling work in #107612 on this evidence, or change
streaming/typing/icon preferences as a substitute for delivery correctness.
The 2026-09-28 correction also adds no requests and suppresses known-window
traffic. Outside a penalty, the configured 1-second send/edit slot permits up
to 60/minute and the independent 2-second typing loop permits 30/minute per
active loop: 90/minute with one loop, above the roughly 60/minute private and
20/minute group envelopes. More concurrent loops increase that sum. The wider
budget in #107612 remains necessary to prevent the first rate-limit event.

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
