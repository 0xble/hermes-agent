# Skill observation files

Load this unit when changing `scripts/curate_skill_observations.py`, the
`hermes observations` CLI, or any writer into `$HERMES_HOME/observations/`.
The patch identity is `skill-observation-files`.

## Behavior

Each observation is one file, `<skill>@<suffix>.md`, written once and never
appended to. The index takes the skill from the stem before `@`, so each file is
its own record: it can be dispositioned and archived without waiting on newer
observations for the same skill. Legacy `<skill>.md` files still index.

Appending to a shared `<skill>.md` made every append a new record that repeated
the older text, and `archive` refused the file because its hash no longer matched
the indexed record.

## Provenance

Fork-only. It extends the fork's observation inbox (`ce9b62321d`), which has no
upstream counterpart. Writer: the dotfiles `skill-source-guard` plugin.

## Verification

`scripts/run_tests.sh tests/scripts/test_curate_skill_observations.py`.
`test_per_observation_files_disposition_and_archive_independently` fails without
the `@` stem parsing.

## Retirement and rollback

Retire with the observation inbox. Rollback reverts this commit. Files named with
`@` would then be skipped by `index` as invalid skill names, not misattributed.
