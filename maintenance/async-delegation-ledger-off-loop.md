# Async delegation ledger off the event loop

Load this unit when changing how the gateway claims, settles, replays, or schedules
durable async-delegation rows (`tools.async_delegation`, `tools.delegation_resume`)
from asynchronous gateway code.

## Required behavior

- Every synchronous `state.db` ledger call reached from the gateway event loop runs in
  a worker thread (`asyncio.to_thread`). That covers the claim, the settle (`complete`,
  `defer`, `release`, `drop`), the sibling batch claim, the startup replay of the launch
  and secondary ledgers, and the boot auto-resume scan, claim, and settle.
- A delivery's primary and sibling settles run in one thread hop
  (`_settle_durable_claims`). If the awaiting task is cancelled, the thread still
  finishes every settle, so no sibling is stranded mid-loop.
- `to_thread` copies the context, so the caller's profile scope (the
  `_HERMES_HOME_OVERRIDE`, secret, and terminal ContextVars) still selects the right
  profile's ledger.
- A cancelled `claim_off_loop` refunds abandoned results without submitting work to
  a closing executor or waiting for a loop callback. Both the inline worker refund
  and the post-handoff thread refund use the separately copied `release_ctx`, so
  claim-local ContextVar mutations cannot redirect the release. Refund failures
  log a warning with exception details while caller cancellation still propagates.
- A batch claim that fails partway refunds the sibling claims it already took, in the worker,
  before the error propagates (`_PartialClaimsError`).
- `settle_durable_claims(on_settled=...)` runs the callback exactly once, in the worker after
  the last settle, never at caller cancellation. With the default executor gone it settles on a
  dedicated non-daemon thread; when no thread can start, nothing settled and the callback does
  not run.
- Cancellation of a delivery is a refund boundary: the primary and every sibling claim
  `defer` instead of `release`, so repeated shutdowns cannot spend attempts. The batch keeps
  ownership of sibling claims until the delivery coroutine starts
  (`_deliver_completion_with_sibling_cleanup`), and a delivery held by `/stop` defers the
  siblings the batch already claimed.
- A primary that never proceeds past pre-flight with no result (claim lost or dropped) defers
  its siblings: the batch requeues them unattempted. Siblings of an attempted, failed injection
  still `release`.
- Admission claims are published (`_pending_completion_acks`) before the turn can start and
  unpublished only from the settle worker. A turn that cannot confirm the owner while the ack is
  still in flight passes that claim to `requeue_admitted_completion`, which reopens the
  still-pending row and clears the claim, so the late acknowledgement is a no-op. Primary and
  sibling identities are marked delivered before the awaited acknowledgement.

## 2026-10-05 incident

The gateway exited twice with code 75, at 17:34 and 17:53 PDT, after the loop-liveness
watchdog saw 3 missed probes. In the 17:53 dump, the `[hermes]` main thread was in
`_async_delegation_watcher` → `_deliver_completion_notification` →
`_settle_durable_claim` → `complete_completion_delivery` → `_update_delivery` →
`sqlite_util.transaction` → `conn.close()`. `state.db` was 35 GB in WAL mode, the disk
was 94% full, and load was about 60, so a commit or close that checkpoints the WAL
blocked the loop. The 17:34 dump hit faulthandler's 100-thread limit before it reached
the main thread, so the cause of that exit is unattributed.

**Upstream comparison (2026-10-05):** no matching issue or PR turned up in searches for
`_settle_durable_claim`, `complete_completion_delivery`, `claim_completion_delivery`,
`claim_event_delivery`, event-loop blocking, and watchdog exit 75. The same synchronous
calls are on `upstream/main` at `56f798641888`. The contribution branch is
`upstream/async-delegation-ledger-off-loop`. The boot auto-resume path exists only in
the fork.

**Upstream PR (2026-10-10):** [PR 133834](https://github.com/NousResearch/hermes-agent/pull/133834)
is a draft at `985238fe8778dcfb05b2027a02490b11aa6ead78`, rebased on upstream `9456670d`
with the shutdown-refund fix. Its exact-head review approved it and local evidence passed. It
cannot leave draft yet: GitHub refuses "Ready for review" because the author has reached the
open pull request limit. It still refused at 37 ready PRs, after PR 106906 went back to
draft. Each maintenance cycle, count the author's open ready PRs. Retry ready once the count is
below 26, or earlier if a retry costs nothing. Rebase and re-review first if upstream moved.
Do not draft other PRs to make room.

## Patch

**Patch identity:** `async-delegation-ledger-off-loop`. Source surfaces:
`gateway/run_notifications_ledger.py` (`settle_durable_claims`, `claim_off_loop`, `claim_siblings_off_loop`), `gateway/run_notifications.py` (
`_preflight_completion_delivery`, `_deliver_completion_notification_scoped`,
`_deliver_async_delegation_group_scoped`, `_deliver_auto_resume_notice`),
`gateway/run_startup.py` (`_start_secondary_profiles`, the auto-resume scheduling call),
and `gateway/run_adapters.py` (`_start_secondary_profile_adapters`).
Proof surface: `tests/gateway/test_completion_delivery.py`
(`test_slow_ledger_transaction_does_not_block_the_event_loop`). It is red on the base,
where the loop stalled 2.60 s behind a 1.2 s fake transaction close. Teardown-refund
proofs in the same file are `test_cancelled_claim_refunds_during_executor_shutdown`
(primary and sibling claims),
`test_cancelled_finished_claim_refunds_after_executor_shutdown`,
`test_cancelled_sibling_claim_releases_its_lease`, and
`test_cancelled_primary_claim_is_refunded_not_released`.
`tests/gateway/test_claim_refund_context.py` additionally proves both refund paths
use the caller's copied release context even if the claim mutates ContextVars,
log release failures with their exception, and keep the teardown thread non-daemon.

The upstream-PR refresh of 2026-10-22 added the last 5 behaviours above (fork port from the
lost-work recovery, fork PR 864). Proofs: `tests/gateway/test_completion_delivery.py`
(`test_cancelled_primary_delivery_refunds_all_claimed_batch_rows`,
`test_primary_claim_failure_defers_already_claimed_batch_siblings`,
`test_lost_primary_claim_refunds_requeued_batch_siblings`,
`test_sibling_claim_failure_refunds_prior_partial_claims`, the `test_settle_callback_*` cases),
`tests/gateway/test_completion_owner_retry.py`
(`test_unconfirmed_owner_while_admission_ack_is_pending_offers_the_completion_again`,
`test_cancelled_delivery_keeps_admission_claim_until_its_ack_finishes`) and
`tests/tools/test_async_delegation_requeue.py`. The upstream PR head is still `7f07e97465`
(2026-10-16): refreshing it onto current upstream is a public action that needs the owner's OK.

**Retire** the shared part once an upstream release includes the contribution and passes
that regression. Keep only the fork-only auto-resume hops.

## Follow-up race fix

The completion pre-flight marks a claim settled before awaiting a terminal `drop` or transient
`release`. The delivery `finally` block skips that primary claim, so cancellation while the
worker-thread settle is blocked cannot issue a competing `release` and strand the completion.
The regression `test_cancelled_preflight_settle_is_not_released_again` covers both dispositions.
