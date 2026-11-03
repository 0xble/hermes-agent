# Browse budget interrupt

Load this unit before changing `_Budget.arm` or the lock-held DDL in
`hermes_state_reconcile`.

## Required behavior

A browse catch-up holds `gateway.lock` only for the drift re-check and the
additive DDL, and that hold ends at `BROWSE_RECONCILE_BUDGET_S`. Gateway boot
waits that budget plus 2s and must not treat the browse as another gateway.

The progress-handler return is not enough. Schema DDL under a slow callback is
about 14s of sleeps if sqlite ignores that return, which is longer than boot's
wait. `arm` also calls `Connection.interrupt` from a daemon thread at the same
deadline. That interrupt is checked on the way back from a callback even when
the callback returns 0, so the statement rolls back and the lock is released
inside the boot wait.

Busy-wait still uses the connection busy timeout. `interrupt` does not abort a
lock wait that never re-enters sqlite.

## Provenance

Fork patch identity: `browse-budget-interrupt`.

Upstream `hermes_state_reconcile._Budget.arm` only installs the progress
handler. No upstream issue or PR was found for a second interrupt when that
return is ignored. The nightly macOS lane failed
`test_slow_browse_reconcile_gives_up_inside_its_budget_and_boot_waits_it_out`
because boot's 5s wait expired while the slow DDL still held the lock.

## Verification

Contract tests: `tests/hermes_cli/test_browse_schema_drift.py::test_browse_budget_interrupts_when_the_progress_handler_does_not`
`tests/hermes_cli/test_browse_schema_drift.py::test_slow_browse_reconcile_gives_up_inside_its_budget_and_boot_waits_it_out`

Rollback: revert the commit carrying `Fork-Patch: browse-budget-interrupt`.
Boot again refuses a browse that outlasts the budget, which is the previous
failure, not a new one.
