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

## Patch

**Patch identity:** `async-delegation-ledger-off-loop`. Source surfaces:
`gateway/run_notifications_ledger.py` (`settle_durable_claims`), `gateway/run_notifications.py` (
`_preflight_completion_delivery`, `_deliver_completion_notification_scoped`,
`_deliver_async_delegation_group_scoped`, `_deliver_auto_resume_notice`),
`gateway/run_startup.py` (`_start_secondary_profiles`, the auto-resume scheduling call),
and `gateway/run_adapters.py` (`_start_secondary_profile_adapters`).
Proof surface: `tests/gateway/test_completion_delivery.py`
(`test_slow_ledger_transaction_does_not_block_the_event_loop`). It is red on the base,
where the loop stalled 2.60 s behind a 1.2 s fake transaction close.

**Retire** the shared part once an upstream release includes the contribution and passes
that regression. Keep only the fork-only auto-resume hops.

## Follow-up race fix

The completion pre-flight marks a claim settled before awaiting a terminal `drop` or transient
`release`. The delivery `finally` block skips that primary claim, so cancellation while the
worker-thread settle is blocked cannot issue a competing `release` and strand the completion.
The regression `test_cancelled_preflight_settle_is_not_released_again` covers both dispositions.
