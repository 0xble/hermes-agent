# Profile plugins

Load this unit when changing the Hermes integration boundary for the personal
profile plugins formerly stored in this checkout. The root contract owns the
fork baseline and publication.

Fork patch identity: `profile-plugin-source-migration` (the local ownership transfer).

## Current ownership

The four authored profile plugins are now maintained in the separate `agents`
repository under `agents/plugins/`:

- `goal-lifecycle`
- `memory-journal`
- `request-update`
- `review-candidate`

They remain Hermes-native projections. Their source, tests, ownership metadata,
and future target build belong to `agents`, not to this Hermes application
checkout. The active profile may continue using installed copies under its
Hermes home during the migration.

## Hermes boundary

This repository owns the Hermes runtime contracts that those projections call,
including goal notices, memory lifecycle hooks, update notifications, review
lifecycle callbacks, plugin discovery, and profile configuration. It does not
own another copy of the profile plugin source.

The compatibility script `scripts/install_candidate_extensions.py` now supports
only `--maintenance-only`, preserving existing scheduled forwarding entry points.
Its plugin-installation path refuses explicitly so an obsolete in-checkout
source tree cannot be recreated.

## Verification

When Hermes runtime contracts change, run the affected Hermes tests from this
repository. When profile plugin behavior or source changes, run the canonical
plugin tests from `agents/plugins/` with the Hermes runtime on the test path.
Verify the installed profile projection separately. A source test or successful
projection build does not establish live gateway acceptance.

## Retirement and rollback

The old in-checkout source tree and its source-local installer/tests are retired.
Rollback of this ownership transfer means restoring the previous source commit
and installer only through an explicitly reviewed change. Do not restore a
second source authority as an operational shortcut.

## Pending migration: canonical-skill-guard

The patch identity `canonical-skill-guard` owns `plugins/canonical-skill-guard/`.
It keeps externally owned canonical skills read-only. It overlaps the
profile-owned `skill-source-guard` (dotfiles `agents/guards/`), and both are
planned to merge into one profile-owned guard. Until then, keep changes here
narrow: bug fixes and manifest correctness only. Verify with
`tests/plugins/test_canonical_skill_guard.py` and
`hermes plugins validate plugins/canonical-skill-guard`. Retire this identity by
deleting the plugin once the merged profile guard is installed, enabled, and
verified live.
