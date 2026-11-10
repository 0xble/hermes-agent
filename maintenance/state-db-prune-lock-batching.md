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

## Operator Prune Window and Single Pass

On 2026-11-07 an agent ran `hermes sessions prune --source subagent|cron --older-than 7 --yes` at 11:00 AM PT against the 107 GB default store. The CLI handed the work to the gateway's shielded `session.prune`, gave up after its 600 s client timeout, and its loop started the next source while the first pass kept running in the gateway. Two passes alternated 5-20 s transactions with a 10 ms gap until a restart at 4:26 PM, and foreground writers (Telegram replies, the delegation retry sweep) waited behind them.

- `hermes sessions prune`, the owner's `session.prune` and `POST /api/sessions/prune` refuse to delete outside `maintenance.deletion_window`, naming the configured window. Dry runs still preview. `--now` (CLI), `now: true` (RPC and web body) is the explicit daytime override.
- A window-bound pass (`prune_sessions(within_deletion_window=True)`: every manual prune without the override and automatic retention) stops between transactions once the window closes and leaves the rest for the next run. Automatic maintenance does not start a VACUUM after the window has closed.
- The CLI sends `now` only when `--now` is given, so a CLI newer than its gateway still prunes inside the window. `session.prune`'s canonical params declare `now`. The dashboard's prune toast shows the refusal reason. The web route reads the window from the dashboard's active profile, not `body.profile`.
- One prune pass per store runs at a time in a process (`hermes_state_prune_pacing.prune_pass`). A second request while the first is still running is refused with `prune_in_progress` instead of starting beside it.

## Per-Transaction Cost

The 25-session transactions were slow because of fixed work, not FTS: messages were already deferred to bounded cleanup pages (`rows[none]` in every slow-write warning), and the warnings showed ~6 s of thread CPU per batch. `retire_prunable` and `retire_local_receipts` each re-read and JSON-decoded every local creation receipt (19.6k rows, 1.15 GB, mostly each cron fire's stored launch request) inside every batch, and `retire_routes` decoded all 41.5k routing entries in Python. Cold pages pushed a batch to 9-22 s of wall time.

Both receipt paths now resolve each id's receipt by primary key through its lineage owner (`local_lineage_owner`, the `sessions.chat_id` pointer that `local_conversation_ids` and explicit deletes already use), and routing entries are filtered in SQL. Known gap, shared with explicit deletes: a reset segment whose `chat_id` was rewritten by a lane move no longer resolves to its owner, so a sweep can take it without the rest of its conversation. The live default store had 0 multi-segment receipts and 0 owner-pointer mismatches on 2026-11-07. Candidates are grouped by local conversation before batching, because `retire_prunable` sweeps a conversation only when all its segments share a transaction. On a synthetic store of the same shape (20k receipts of ~58 KB, 40k routes), a 25-session `retire_prunable` went from 2.5 s to 0.02 s with the same receipts and routes retired. Each pass also sizes its next batch by measured hold time (`PrunePacer`: halve above 0.5 s, down to 1 session) and pauses at least as long as it held the lock (0.05-2 s).

## Bounded Deletion

Session-count pruning batches alone do not bound a large transcript's FTS-trigger work. Explicit single, bulk/chain and runtime deletes, and retention pruning, now publish the existing durable retirement fence before reclaiming messages. Guard, expected-id and expected-display checks remain inside that retirement transaction. Public session, transcript, timeline and search reads exclude the retired ids, including hidden/archive and LIKE fallback routes. Late transcript appends and child creation reject a retired id or parent.

`hermes_state_deletion.py` extends the same retirement marker with pending cleanup, rather than adding a schema or independent deletion identity. Each message transaction removes at most 16 rows and 64 KiB of payload, admitting an oversized row alone. Length enumeration uses a reader outside the writer lock. Deletion executes in single-row steps, stops after the 100 ms work target, commits, and yields before reacquiring the writer. All existing FTS delete triggers remain enabled.

Each retention pass creates a fresh budget for its own store, never a shared budget across served profiles. `max_seconds`, when supplied, and the end of an admitted deletion window bound candidate reads, SQLite VM work, cleanup pages and transcript unlinks. Without either bound the pass has no total runtime deadline, but each write transaction still has a 250 ms cap. The window admission policy remains authoritative: a closed fallback specification cannot turn an admitted always-open policy into an already-expired deadline. SQLite progress handlers interrupt expensive statements, including FTS triggers, and interrupted pages roll back with durable cleanup markers retained. An OS fsync or blocked filesystem operation cannot be preempted.

The next explicit delete or retention prune resumes pending work. Spillover pointers are retired per page before their rows disappear. Session rows are removed only after their messages, and pending markers cannot expire or be replaced by another sweep. Each page rechecks its original marker so a delayed cleaner cannot delete a later generation. System-prompt cleanup remains separately bounded.

Runtime session resolution and the shared compression/branch/reset child-row publisher check the existing retirement fence inside their write transaction. A retained physical row cannot authorize new admissions, workers or child publication. Pending cleanup isolates and logs each marker's integrity failure, retaining its fence and retry work rather than disabling a guard. Unrelated deletes and prunes continue, but failures reclaiming the caller's requested ids still propagate after the pass.

`tests/hermes_state/test_session_delete_batches.py` covers a 1,025-message single/chain/prune regression, another SQLite writer committing between pages, interruption immediately after retirement and after a committed page, restart/next-prune recovery, invisibility through FTS and LIKE, propagated database errors, pending-marker expiry, snapshot fences, and message-byte bounds. Existing guard, cascade, compression, retirement, media and prune tests remain authoritative.

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

`gateway/run.py`, `gateway/session_prune.py`, `hermes_cli/cli_auto_maintenance.py`, `hermes_cli/maintenance_window.py`, `hermes_cli/sessions_cmd.py`, `hermes_cli/web_routers/sessions.py`, `hermes_state_prune_pacing.py`, `hermes_state_mutation_retirement.py`, `hermes_state_local.py`, `hermes_state_maintenance.py`, `hermes_state.py`, `hermes_state_transactions.py`, `hermes_state_messages.py`, `hermes_state_compression.py`, `hermes_state_message_archive.py`, `hermes_state_write_metrics.py`, `hermes_state_lockowners.py`, and their focused tests.

## Focused regression

```text
scripts/run_tests.sh tests/hermes_state/test_prune_pass_bounds.py tests/hermes_cli/test_sessions_prune_window.py tests/gateway/test_state_db_maintenance_window.py tests/hermes_cli/test_session_vacuum_config.py tests/hermes_state/test_hermes_state.py tests/hermes_state/test_write_lock_owner_attribution.py tests/hermes_state/test_compaction_write_metrics.py tests/hermes_state/test_transcript_write_diagnostics.py tests/hermes_state/test_compression_lease_refresh_before_publish.py
```

## Retirement

Remove this patch when upstream retains the deletion-window gate (automatic and operator prune, with an explicit override), single-pass prune per store, receipt retirement without full receipt scans, bounded automatic prune transactions with guard-safe lineage handling, and cross-platform long-lock diagnostics.

## Rollback

Revert the commit carrying `Fork-Patch: state-prune-lock-batching`.
