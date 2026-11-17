# UI Preferences Store Lock

Load this unit when changing the machine-level UI preferences lock shared by the
native Desktop and Webapp writers.

## Required Behavior

Fork patch identity: `ui-preferences-store-lock`.

A writer waits for a live lock owner to release the directory, regardless of
wall-clock duration. Stale recovery checks the owner PID and atomically renames
an abandoned lock aside before removing it, so competing stale recoverers cannot
remove a newly acquired lock.

## Contract Tests

Contract tests: `tests/hermes_cli/test_ui_preferences.py::test_lock_waits_for_a_slow_holder_instead_of_failing_after_a_wall_clock_budget`

## Verification

Run `env -u HERMES_HOME TMPDIR=/private/tmp/x.XXXX /Users/brianle/Repos/hermes-agent/.venv/bin/python -m pytest tests/hermes_cli/test_ui_preferences.py -p no:cacheprovider -o addopts=''`.
Run the focused Electron `ui-preferences.test.ts` suite through the desktop
package's `test:desktop:platforms` script.

## Retirement And Rollback

Retire this correction when upstream provides equivalent cross-process lock
ownership and atomic stale recovery. Revert the commit carrying
`Fork-Patch: ui-preferences-store-lock` to roll back the correction.
