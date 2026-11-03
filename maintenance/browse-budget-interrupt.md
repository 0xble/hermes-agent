# Browse budget interrupt

Load this unit before changing `_apply_additive_schema` or the lock-held work
in `hermes_state_reconcile`.

## Required behavior

A browse catch-up holds `gateway.lock` for the drift re-check and additive
schema catch-up. Its SQLite work is bounded by `BROWSE_RECONCILE_BUDGET_S`.
Gateway boot waits that budget plus 2s, not mistaking the browse for a runtime
owner. Keep the existing ownership refusal for real gateways.

Install the budget progress handler before any connection setup SQL, including
macOS durability PRAGMAs. Those PRAGMAs may initialize the schema on a fresh
connection; previously they ran before `budget.arm`, so that work could hold
`gateway.lock` beyond boot's wait. They are no-ops on non-macOS hosts, and the
schema-initialization cost varies across SQLite builds.

The durability helpers intentionally suppress SQLite errors. Check the budget
after they return, before `BEGIN IMMEDIATE`, so a swallowed interruption cannot
proceed into DDL. The callback return still interrupts SQLite; no independent
watchdog or timeout increase is needed. Busy waits retain the existing timeout.

## Provenance

Fork patch identity: `browse-budget-interrupt`.

The base checkout called `_reapply_durability_barriers` before `budget.arm`.
Nightly macOS run 41639452032 at 557d229a failed the existing slow-browse/boot
contract: the browse kept its lock past boot's 5s wait. A deterministic
regression advances a clock from a real, chained SQLite progress callback
during barrier setup: the base lets setup finish unbounded; the fix interrupts
it before beginning a transaction. This does not assume SQLite ignores a
nonzero callback return.

## Verification

Contract tests:
- `tests/hermes_cli/test_browse_schema_drift.py::test_browse_budget_covers_durability_setup_before_begin`
- `tests/hermes_cli/test_browse_schema_drift.py::test_slow_browse_reconcile_gives_up_inside_its_budget_and_boot_waits_it_out`

The regression retains the best-effort durability wrapper and asserts the
setup statement was interrupted, no `BEGIN` ran, and the database bytes/schema
were unchanged. Its clock is deterministic, not a widened real-time allowance.

Rollback: revert this patch's changes. The previous unbudgeted setup interval
returns; it is not an alternative way of satisfying the boot-wait contract.
