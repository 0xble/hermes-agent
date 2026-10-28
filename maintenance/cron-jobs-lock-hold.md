# Cron Jobs Lock Hold

Load this unit when changing the cron jobs advisory lock.

## Required Behavior

Fork patch identity: `cron-jobs-lock-hold`.

Preserve cross-process exclusion and nested lock reuse. Open the lock file in
`a+` mode with plain `utf-8`, consistent with the source checker's write policy.
The lock currently seeks and locks the descriptor without reading or writing
text. BOM-tolerant decoding provides no benefit here and must not introduce a
BOM if lock metadata is written later.

## Verification

Run `python scripts/check-windows-footguns.py --all` and
`scripts/run_tests.sh tests/cron/test_jobs_crossprocess_lock.py`.

## Retirement And Rollback

Retire this correction when the selected upstream release opens this writable
lock without a BOM-emitting encoding. Revert the commit carrying
`Fork-Patch: cron-jobs-lock-hold` to roll back the correction.
