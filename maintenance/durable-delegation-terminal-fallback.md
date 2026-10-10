# Durable delegation terminal fallback

Load this unit when changing how async delegations persist child-failure
notices or terminal results that could not be written to the lifecycle row
(`tools/async_delegation.py::_persist_outbox_event`, `_persist_completion`,
`restore_undelivered_completions`), or how session recovery copies the
`async_delegation_events` table.

## Required behavior

- An interim child-failure notice (`task_failure`) and a terminal result whose
  lifecycle write raised (`terminal_fallback`) are stored as durable outbox
  rows in `async_delegation_events`, each with its own event id and delivery
  state. Restart and orphan recovery replay them like lifecycle rows.
- A `terminal_fallback` row claims the delegation lifecycle row in the same
  transaction as its outbox insert. A zero-row conditional transition rolls
  the insert back, so a competing terminal writer cannot create a redundant
  deliverable.
- Ambiguous completion writes reconcile the lifecycle payload and fallback
  outbox before queueing. The authoritative payload is delivered once; a losing
  terminal writer does not enqueue its result beside the winner. If durable
  state is unavailable or absent, the real result remains an explicitly
  in-memory-only event without a phantom outbox identity.
- Index-less task notices get collision-safe event ids.
- Session recovery registers `async_delegation_events`, so a rebuilt
  `state.db` keeps undelivered outbox rows.

## Why

A child failure notice or a terminal result that failed to persist was
in-memory only. If the owning process exited before delivery, the parent never
learned the unit failed, and a dead-owner restart reported it as `unknown`
instead of its real result.

## Provenance

Fork patch identity: `durable-delegation-terminal-fallback`.

Upstream-owned code. No upstream issue or PR covered durable outbox delivery
for child failure notices or terminal-write fallbacks when this patch landed.
The gateway settlement side is owned by
[Async delegation ledger off the loop](async-delegation-ledger-off-loop.md),
and the interim notice contract by
[Internal notification silence](internal-notification-silence.md).

## Resolved race coverage

- A commit-then-raise completion is reconciled against the terminal lifecycle
  row before fallback creation, so it cannot gain a second outbox replay.
- A competing terminal transition owns the durable payload, including a winner
  committed after the initial active-state read. Raised and zero-row writes use
  the same conditional fallback path, and fallback exceptions reconcile again
  before offering an event. The losing worker does not enqueue a notification.
- A fallback commit-then-raise recovers its existing outbox delivery identity
  before queueing, so acknowledging the live copy also settles restart replay.
- A fallback insert and its lifecycle claim are one conditional transaction.
- Ordinary terminal failures, interim notices, duplicate fallback invocation,
  and unavailable/missing persistence retain their existing behavior and are
  covered by focused regression tests.

## Known gaps

- A stale pending `task_failure` notice can replay after the unit's final
  result on restart. This patch does not resolve interim-notice ordering.

## Verification

Run `scripts/run_tests.sh tests/tools/test_async_terminal_fallback_ownership.py tests/tools/test_async_batch_task_failure_notice.py tests/hermes_cli/test_session_recovery.py tests/gateway/test_completion_delivery.py`.

## Retirement and rollback

Retire when upstream persists interim and fallback delegation events durably.
To roll back, revert the patch commits. Existing `async_delegation_events`
rows are then ignored.
