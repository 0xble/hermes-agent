# Cron Jobs Lock Hold

Load this unit when changing cron jobs-lock scope, completion-proof ledger reads, or lock-owner diagnostics.

## Required Behavior

- Fork patch identity: `cron-jobs-lock-hold`.
- Completion proof on an established ledger uses a read-only SQLite connection, not the execution writer mutex, schema initialization, or WAL-mode mutation. Due scans share a 50 ms deadline and stop further ledger reads after the first busy timeout. Reads outside a scan retain a 50 ms busy timeout. A committed completed slot still suppresses duplicate execution while another execution transaction is open.
- Preserve legacy lazy schema migration and the existing unknown-proof fail-open policy. A missing ledger must not be created by a completion read.
- Keep the cross-process jobs lock and its 30-second timeout. On acquisition, publish the owner's PID, entry-point command, operation, and monotonic start in the held lock file. Nested calls reuse the owner. Waiters never overwrite it. Timeouts report the owner and elapsed hold duration, or explicitly report unavailable metadata for older holders. Clear owner metadata before unlocking, including exceptional exits. Log completed holds of at least 5 seconds.
- Diagnostics are best effort and must not prevent locking or execute arbitrary command-line arguments in logs.
- No save may proceed without cross-process ownership. Timeout, missing backend, and locking errors refuse ordinary, nested, repair, and exact-document writes with a loud exception. Read-only degraded sections remain available. An acknowledged CLI edit cannot be reverted by a degraded scheduler or external worker.
- Loaded snapshots retain their own baseline. A later save rebases only fields actually changed by that caller onto the locked current store. Unchanged delivery, activation, payload, and schedule fields remain current. Conflicting same-field edits and deleted jobs fail closed rather than overwrite or resurrect. Explicit `replace=True` is reserved for intentional recovery, still requiring the cross-process lock.
- Only real acquisition timeouts raise `JobsLockContended(RuntimeError)`. Missing backends and open/flock errors remain OSErrors on the unwritable-store notice path. Health probes must open `.jobs.lock`. Idle recovery defers contention without traceback and retries next tick. Repairs and due scans defer persistence, the pre-dispatch advance defers the entire tick without filtering recurring jobs, and a refused fire claim settles only its unstarted attempt. All retain the stored job and occurrence for the next tick. Log warnings with holder attribution, never mark a busy store unwritable or emit an unwritable transition notice.
- The next locked scan clears a locally owned one-shot run claim when no worker is running here, retaining outage eligibility. Running workers and foreign claims stay protected.
- Scheduler workers receive an execution payload but completion, claim renewal, and due-scan bookkeeping reload the jobs store before saving. Never persist the dispatch-time job dictionary as a replacement definition.
- Long contention intentionally trades availability for ownership safety. Each write refuses after the 30-second lock bound. A running fire claim that cannot renew for the 180-second heartbeat grace is interrupted as uncertain, rather than continuing with an unverified lease. After an execution result is committed, `mark_job_run` can still refuse, leaving `last_run_at` and other jobs.json bookkeeping stale until retried after contention ends. A retry reloads the store and can persist the completed run without replaying its work. This patch does not add an automatic post-commit retry queue.

## Evidence And Verification

On 2026-10-27, read-only Python-frame samples identified gateway PID 95311's scheduler thread inside `get_due_jobs` → `completed_occurrence` → execution transaction/schema setup while holding the jobs lock. A copied-store due scan took 224 writer-style ledger reads. Holding an unrelated execution transaction in the same process made the real due-scan critical section last 35.015 seconds. The read-only completion path reduced the same controlled hold to 0.026 seconds.

`tests/cron/test_jobs_lock_failures.py` covers a chmod-0 lock file, the unwritable transition,
non-contention flock errors, and idle-recovery contention. The contention suite covers next-tick
one-shot retry. The lock-hold suite checks the configured SQLite timeout deterministically,
one shared deadline, one busy read per scan, and recovery on the next scan.

Run each touched test file in its own pytest process to avoid module-reload class mismatches:

```sh
env -u TMPDIR -u TEMP -u TMP DIRENV_DISABLE=1 HERMES_HOME=$(mktemp -d) .venv/bin/python -m pytest -q -p no:cacheprovider tests/cron/test_jobs_lock_failures.py
```

Broader verification:

```sh
env -u TMPDIR -u TEMP -u TMP DIRENV_DISABLE=1 HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh tests/cron
env -u TMPDIR -u TEMP -u TMP DIRENV_DISABLE=1 HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh tests/e2e/core/cli_tmux/test_classic_cli_live_session.py --file-timeout 900
```

## Retirement And Rollback

No matching cron reliability unit existed when this patch was added. Retire when the selected upstream release provides bounded read-only completion reads, fail-closed store writes, field-diff snapshot saves, and equivalent real-writer/owner-diagnostic regressions. Revert the commits carrying `Fork-Patch: cron-jobs-lock-hold` to roll back without changing the jobs store or ledger format. Older releases ignore diagnostic lock-file contents.
