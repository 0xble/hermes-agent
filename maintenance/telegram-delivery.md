# Telegram delivery remnants in shared code

The fork's Telegram adapter patches were retired, and the adapter is stock upstream
(see [Telegram stack retirement](telegram-stack-retirement.md)). This unit owns the
Telegram-conditional behavior that stayed in shared delivery code because other
families still rely on it. Load it when changing reply anchors, the delivery ledger,
outbound-class labels, or cron live-lane flood handling.

## Required behavior

- Synthetic gateway event IDs may identify notifications but never become Telegram reply
  anchors. Numeric Telegram message IDs still anchor ordinary DM-topic replies
  (`_telegram_reply_anchor` in `gateway/platforms/base.py`).
  - Patch identity: `telegram-delivery`.
- Rejected deliveries back off and preserve recovery; a partial delivery does not trigger a
  duplicate fallback (`gateway/delivery_ledger.py`).
  - Patch identity: `slice-10-delivery-ledger`.
- Non-final sends are labelled through `outbound_class` (`OUTBOUND_NOTICE`, progress) at their
  call sites, and unlabelled sends are finals. Nothing reads the label since the durable outbox
  was removed ([Durable outbox retirement](durable-outbox-retirement.md)). Removing the labels is a
  follow-up.
  - Patch identity: `telegram-chat-budget`.
- The shared splitter derives its `(i/n)` indicator reserve from the source codepoint length, so
  arbitrary chunk counts never overflow the caller's limit (`BasePlatformAdapter.truncate_message`).
  Slack and Discord split regressions cover it in `tests/gateway/test_slack.py` and
  `tests/gateway/test_discord_split_cap.py`.
  - Patch identity: `telegram-chunk-indicator-budget`.

## Cancelled Final Settlement

A captured successful final stays delivered when its turn is cancelled. A cancelled
refusal or an interrupted transport stays attempting for the existing ambiguous-send
recovery, without running refusal callbacks or submitting a failure write first.
Cancellation is delivered at a real suspension, not at a coroutine return. The send
wrapper therefore settles a known success synchronously, then returns through
`records_delivery` so the captured result is counted exactly once. After resetting
the settlement ContextVar, `send_final_ledgered` yields through `asyncio.sleep(0)`
only when cancellation is pending, before refusal callbacks or failure finalization.

This repairs the fork-local settlement introduced by #704. Current upstream guidance
was checked at `dd5bc7ee13f12c6fbf70ca47d05135f63e76258a`. Related upstream
[#107135](https://github.com/NousResearch/hermes-agent/pull/107135) owns replay
claim/send/refund cancellation, and
[#105810](https://github.com/NousResearch/hermes-agent/pull/105810) owns polling
recovery, not this final-producer checkpoint. Reuse the existing ContextVar and
ledger state machine rather than importing another recovery lifecycle.

`test_cancel_after_captured_send_preserves_accounting_and_settlement` forces a real worker
to finish before cancellation is delivered and drains that worker before observing
the row. Both captured success and refusal count exactly 1 delivery metric. The test
also proves no failure writer or non-suspending refusal callback ran.
The adjacent success, interrupted-send and concurrent-obligation tests retain their
contracts. Retire this adaptation when released upstream provides equivalent
final-producer cancellation ordering. Roll back only this checkpoint and its test
adaptation, without rewriting ledger rows.

## Cron Short Flood Wait

**Patch identity:** `cron-short-flood-wait`. On
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

## Verification

`scripts/run_tests.sh tests/gateway/test_delivery_ledger.py tests/gateway/test_delivery_flood_invariants.py
tests/cron/test_cron_live_delivery_confirmation.py
tests/gateway/test_telegram_thread_fallback.py tests/gateway/test_slack.py tests/gateway/test_discord_split_cap.py`.

## Retirement and rollback

Retire each remnant with its owning family: the outbound-class labels once their call sites
are removed, the cron items when upstream cron delivery stops falling back to a second sender during an
active flood deadline, and the reply-anchor guard when upstream stops using synthetic IDs
as Telegram reply targets. Roll back by reverting the logical patch; no persistent data changes.
