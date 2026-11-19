# UI Preferences Store Lock

Load this unit when changing the machine-level UI preferences lock shared by the
native Desktop and Webapp writers.

## Required Behavior

Fork patch identity: `ui-preferences-store-lock`.

A writer waits for a live lock owner to release the directory, with a generous
30-second overall backstop for a stuck writer. The backstop is not a contention
retry or flake fix: it preserves the existing poll interval and fails with
`PreferencesBusy`/`preferences store is busy` rather than waiting forever.
Stale recovery checks the PID from the sampled owner token and checks that
token again before moving the directory. A change returns to the wait loop.
Both writers reclaim a
live-looking owner whose owner file is older than ten minutes. The age backstop
is the shared protection against PID reuse. Old `<pid>-<random>` tokens remain
readable during rolling upgrades. A lock directory with no owner file (a writer
that died between creating the directory and writing its owner) ages by the
directory's own mtime and is reclaimed after ten seconds. Stale locks are atomically renamed aside
before removal. If the owner token changed, preserve the quarantined lock, restore it if possible,
and retry acquisition within the original wait budget rather than reporting busy immediately.

PID liveness is platform-dependent: zombies are treated as dead by the Python
gateway helper, and PID namespaces can make a PID visible in one process and
not another. The age backstop remains the protection when a start identity is
unavailable or a PID has been recycled.

## Contract Tests

Contract tests: `tests/hermes_cli/test_ui_preferences.py`.

The live-owner, dead-PID, age-backstop and bounded-wait cases also run in
`apps/desktop/electron/ui-preferences.test.ts`.

## Verification

Run `env -u HERMES_HOME -u __HERMES_ACTIVATED bash scripts/run_tests.sh
--file-retries 0 tests/hermes_cli/test_ui_preferences.py`.
Run `cd apps/desktop && pnpm exec vitest run --project electron
 electron/ui-preferences.test.ts` and the changed-file ESLint check.

## Retirement And Rollback

Retire this correction when upstream provides equivalent cross-process lock
ownership and atomic stale recovery. Revert the commit carrying
`Fork-Patch: ui-preferences-store-lock` to roll back the correction.
