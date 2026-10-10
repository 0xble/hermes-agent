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
- A `terminal_fallback` row marks the delegation lifecycle row terminal in the
  same transaction, so owner-death recovery does not also replay a synthetic
  `unknown` completion for the same unit.
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

## Known gaps

- If a competing terminal transition already won, the conditional lifecycle
  update matches no row but the fallback outbox row still commits, so the
  parent can receive two completions. This needs the first terminal write to
  raise, so it is rare.
- A stale pending `task_failure` notice can replay after the unit's final
  result on restart.

## Verification

Run `scripts/run_tests.sh tests/tools/test_async_batch_task_failure_notice.py
tests/hermes_cli/test_session_recovery.py tests/gateway/test_completion_delivery.py`.

## Retirement and rollback

Retire when upstream persists interim and fallback delegation events durably.
To roll back, revert the patch commits. Existing `async_delegation_events`
rows are then ignored.
