# Cron Jobs Lock Hold

Load this unit when changing cron jobs-lock scope, completion-proof ledger reads, or lock-owner diagnostics.

## Required Behavior

- Fork patch identity: `cron-jobs-lock-hold`.
- Completion proof on an established ledger uses a read-only, non-waiting SQLite connection, not the execution writer mutex, schema initialization, or WAL-mode mutation. This applies to both due scans and fire claims. A committed completed slot still suppresses duplicate execution while another execution transaction is open.
- Preserve legacy lazy schema migration and the existing unknown-proof fail-open policy. A missing ledger must not be created by a completion read.
- Keep the cross-process jobs lock and its 30-second timeout. On acquisition, publish the owner's PID, entry-point command, operation, and monotonic start in the held lock file. Nested calls reuse the owner. Waiters never overwrite it. Timeouts report the owner and elapsed hold duration, or explicitly report unavailable metadata for older holders. Clear owner metadata before unlocking, including exceptional exits. Log completed holds of at least 5 seconds.
- Diagnostics are best effort and must not prevent locking or execute arbitrary command-line arguments in logs.
- No save may proceed without cross-process ownership. Timeout, missing backend, and locking errors refuse ordinary, nested, repair, and exact-document writes with a loud exception. Read-only degraded sections remain available. An acknowledged CLI edit cannot be reverted by a degraded scheduler or external worker.
- Loaded snapshots retain their own baseline. A later save rebases only fields actually changed by that caller onto the locked current store. Unchanged delivery, activation, payload, and schedule fields remain current. Conflicting same-field edits and deleted jobs fail closed rather than overwrite or resurrect. Explicit `replace=True` is reserved for intentional recovery, still requiring the cross-process lock.
- Scheduler workers receive an execution payload but completion, claim renewal, and due-scan bookkeeping reload the jobs store before saving. Never persist the dispatch-time job dictionary as a replacement definition.

## Evidence And Verification

On 2026-10-27, read-only Python-frame samples identified gateway PID 95311's scheduler thread inside `get_due_jobs` → `completed_occurrence` → execution transaction/schema setup while holding the jobs lock. A copied-store due scan took 224 writer-style ledger reads. Holding an unrelated execution transaction in the same process made the real due-scan critical section last 35.015 seconds. The read-only completion path reduced the same controlled hold to 0.026 seconds.

Run:

```sh
env -u TMPDIR -u TEMP -u TMP DIRENV_DISABLE=1 HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh tests/cron
env -u TMPDIR -u TEMP -u TMP DIRENV_DISABLE=1 HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh tests/e2e/core/cli_tmux/test_classic_cli_live_session.py --file-timeout 900
```

## Retirement And Rollback

No matching cron reliability unit existed when this patch was added. Retire when the selected upstream release provides non-waiting completion reads, fail-closed store writes, field-diff snapshot saves, and equivalent real-writer/owner-diagnostic regressions. Revert the commits carrying `Fork-Patch: cron-jobs-lock-hold` to roll back without changing the jobs store or ledger format. Older releases ignore diagnostic lock-file contents.
