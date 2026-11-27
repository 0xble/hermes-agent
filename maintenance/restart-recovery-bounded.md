# Bounded restart recovery

Fork patch identity: `restart-recovery-bounded`.

## Contract

The two recovery steps every gateway start runs before it serves must not hold the
state.db writer for a scan over history.

- `recover_session_inputs` settles only rows a lost owner left in flight: `started`
  admissions, `terminal` admissions still marked `steering`, and live (`status !=
  'terminal'`) worker executions. Each statement names its partial index's WHERE
  terms verbatim (`idx_session_admissions_started`, `idx_session_admissions_steering`,
  `idx_worker_executions_live` in `hermes_state_common.SCHEMA_SQL`), so SQLite can
  prove the index applies. Without them it scanned the whole wide admissions table,
  whose status, outcome and owner_epoch columns sit behind the payload and lineage
  overflow pages.
- `release_handed_off_receipts` finds receipts a messaging lane took over on a pooled
  reader. It takes the writer only when there are candidates, and the writer re-checks
  just those owners (`moved_receipts(..., owners=...)`) before handing them off.

On 2026-11-14 these held the writer 19.4 s and 32.6 s at one restart (up to 20.5 s and
70.0 s on 2026-11-11), so no message, receipt or lease could be written for about 50 s
after each start. Both were I/O-bound (callback CPU 1.2 s and 4.3 s), and the receipt
scan committed nothing.

## Provenance and upstream disposition

Fork-only: the runtime admissions ledger and local receipts are fork features. The
indexes are additive `CREATE INDEX IF NOT EXISTS` statements that the declarative
schema pass creates on every writable open. Building them on an existing state.db
costs one scan at the first start after the upgrade. Older builds ignore them.

## Maintenance decision

Keep any new restart-recovery statement on an index whose WHERE terms it repeats, and
keep read-only discovery out of the write transaction. If the in-flight statuses or
the `STEERING` outcome change, change the partial indexes with them: the planner test
fails when a statement stops matching its index.

Contract tests: `tests/persistence_sqlite/core/test_restart_recovery_bounded.py`,
  `tests/gateway/test_handoff_local_receipt.py`.

Rollback is code-only. The partial indexes may stay: nothing else reads them.
