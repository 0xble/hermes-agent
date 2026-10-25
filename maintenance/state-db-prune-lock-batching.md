# State.db prune lock batching

## Scope

Automatic session retention pruning is destructive background maintenance. It must not spend its daily throttle on a daytime run or hold SQLite's WAL write lock while deleting an unbounded session/message/FTS set.

Fork-Patch-Backfill: dd8b51a7f52d926fc084cd7e84bc8b72a651ff0f; state-prune-lock-batching

## Contract

- The automatic-prune function itself and its gateway/CLI callers run only when `maintenance.deletion_window` is open. Outside the window they return before touching the state database or spending throttle markers, so alternate entrypoints cannot bypass the gate.
- Retention pruning selects candidates read-only, then rechecks each bounded batch inside its own idempotent write transaction. Active write guards protect the complete compression-descendant lineage, not only the rows in the current batch.
- Each batch commits before the next batch begins and yields briefly so foreground writers can acquire the lock. Unreferenced system prompts are also removed in bounded transactions.
- `_execute_write_marked` emits a warning when one acquired transaction holds the lock for several seconds, including the originating callback, process id, bounded transcript target and input-row count when known. Acquisition time is separate from the lock-hold threshold. Callback wall/thread-CPU time, commit/rollback time and settlement outcome distinguish expensive work from a failed or slow commit without logging transcript payloads or SQL.
- The same attempt-local `WriteMetrics` context retains archive/mark-inactive, insert (including inline FTS), clone (including inline FTS), display-reconcile, callback-total and commit/rollback timings, with affected message-row counts. Callback-total includes the named callback phases, so those durations must not be summed. Failed transactions report attempted row writes, not committed totals. Fast transactions add no log lines or diagnostic SQL. On macOS, the lock-owner diagnostic says `/proc/locks` is unavailable rather than claiming that a holder released just before the deadline.

## Compaction Attribution

The existing slow-write warning also brackets in-lock compaction preparation
(`compaction_prepare`: lease/history guards, model-config merge and coverage proof),
`archive_enumerate` (active/carried/tail selection and rewind preparation),
`archive_timestamp_carry` and the final `session_counters` update. Both the
watermark and proved-coverage paths use these buckets. Disjoint enumeration
regions accumulate into the same attempt-local bucket.

`display_reconcile` contains `display_index` and the planned or full fold. Within
`display_index`, `display_identity_enumerate` and `display_backfill_enumerate`
each include SQL execution, cursor stepping, SQLite-row materialization and
dictionary insertion. `display_materialize` covers sorting ids and building the
unique result list. Each of these 3 buckets also emits a `_cpu` duration from
`time.thread_time()`, sampled around the whole region, never per row. The optional
CPU measurement uses the existing phase map and is a no-op without an active
metric context. These are Python/SQLite region measurements, not SQL-only timings.
The identity cursor uses the identity index. The backfill cursor uses its partial
index to find candidates but reads stored identity/order fields from the table.
Neither cursor reads message payloads.

`display_identity_rows` and `display_backfill_rows` count cursor rows, including
retained active or compacted history, not just this compaction's input. A row with
a non-NULL identity and NULL order occurs in both cursors.
`display_materialized_rows` counts unique rows after dictionary deduplication. Thus
the cursor counts may sum to more than the unique count. Completed enumeration
counts in a failed transaction describe attempted work, not committed rows.

All of these buckets are inside `callback_total`, and the display buckets are
nested further. Do not sum parent and child durations. Thresholds, outer callback
thread-CPU timing, rollback behavior and metric isolation remain unchanged. No
extra diagnostic queries, SQL tracing, row payloads or per-row clocks are added.
Wall/CPU separation distinguishes elapsed time from CPU work by this thread,
not the specific reason for a gap. These measurements do not establish an I/O,
GIL, scheduling or index bottleneck, and do not claim a latency improvement.

## Source surfaces

`gateway/run.py`, `hermes_cli/cli_auto_maintenance.py`, `hermes_state_maintenance.py`, `hermes_state.py`, `hermes_state_transactions.py`, `hermes_state_messages.py`, `hermes_state_compression.py`, `hermes_state_message_archive.py`, `hermes_state_write_metrics.py`, `hermes_state_lockowners.py`, and their focused tests.

## Focused regression

```text
scripts/run_tests.sh tests/gateway/test_state_db_maintenance_window.py tests/hermes_cli/test_session_vacuum_config.py tests/hermes_state/test_hermes_state.py tests/hermes_state/test_write_lock_owner_attribution.py tests/hermes_state/test_compaction_write_metrics.py tests/hermes_state/test_transcript_write_diagnostics.py tests/hermes_state/test_compression_lease_refresh_before_publish.py
```

## Retirement

Remove this patch when upstream retains the deletion-window gate, bounded automatic prune transactions with guard-safe lineage handling, and cross-platform long-lock diagnostics.

## Rollback

Revert the commit carrying `Fork-Patch: state-prune-lock-batching`.
