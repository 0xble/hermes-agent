# Cron execution WAL lifetime

Patch identity: `cron-execution-wal-lifetime`.

## Contract

The built-in ticker owns one idle execution-ledger connection for each served
profile, independently of the short-lived work connections. Work handles still
commit/roll back and close deterministically; the owner holds no read or write
transaction. Closing each work handle must not be SQLite's last-connection close,
which checkpoints and removes WAL/SHM and breaks read-only opens on SQLite builds
that cannot create shared memory through a read-only connection.

`cron/scheduler_store.py` opens through the ledger's ordinary initializer and WAL
fallback under the existing profile scope. Owners close when a home leaves the
multiplex served set and when the ticker returns, including exceptional exits.
Acquisition failures retain the existing per-profile error containment and EMFILE
backoff: acquisition is attempted before work, but a failure is reported only
after the work tick, preserving degraded-store orphan cleanup. Failed profile
enumeration does not evict existing owners; a successfully enumerated empty set
does. Gated-out homes never acquire an owner, and newly admitted homes acquire
before their first work tick.
No readers are upgraded to read/write, and no WAL safety checks are loosened.

## Provenance

The live report concerned `<home>/cron/executions.db`, not `<home>/executions.db`.
A read-only 30-open, timeout-zero probe found 28 `unable to open database file`,
one `database is locked`, and one successful open. A concurrent 20-second lsof
sample caught gateway PID 23062 holding DB/WAL/SHM briefly and no holder between
work cycles. No live database was opened read/write for this investigation.

The 118ba6a24dca..beed66c450b3 release diff does not change the ledger connection
or journal-mode/transaction paths. This is not a demonstrated regression in that
release range. Deterministic close was introduced by upstream commit
1d721a66f7fc63e5e05a9cd8412dce3fc366db5a, already in both releases, and the same
last-close churn is documented in upstream issue #133883. Upstream PR #134532
reduces opens per scan but closes at scan end, so it does not preserve idle WAL
availability. Do not restore the original connection leak as a workaround.

## Proof and retirement

Contract tests: `tests/cron/test_scheduler_store.py`.

The regression drives the real `InProcessCronScheduler.start` lifetime in single
and multiplex mode, cycles ordinary ledger transactions, then opens a fresh
`mode=ro` reader after work and during idle waits. It checks WAL/SHM presence too,
so SQLite builds that can create these through read-only handles cannot hide the
regression. Separate cases cover normal shutdown, departure, committed writes,
failed initialization cleanup/retry, and housekeeping/backoff on owner failure.

Run `scripts/run_tests.sh --file-retries 0 tests/cron/test_scheduler_store.py`
and the ticker startup, provider, ledger, EMFILE, and drift suites before changing
this lifecycle. Retire when upstream keeps WAL/SHM available between work cycles
and these regressions pass without this patch. Roll back by reverting the commit
carrying `Fork-Patch: cron-execution-wal-lifetime`; this restores idle reader
failures but leaves ledger contents/schema unchanged.
