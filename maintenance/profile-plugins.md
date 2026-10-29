# Profile plugins

Load this unit when changing the Hermes integration boundary for the personal
profile plugins formerly stored in this checkout. The root contract owns the
fork baseline and publication.

Fork patch identity: `profile-plugin-source-migration` (the local ownership transfer).

Fork patch identity: `canonical-skill-observation-intake` (legacy bundled guard).
The retained `canonical-skill-guard` validates observation leaf names, opens the
inbox and files without following symlinks, and binds writes to directory handles.
An unsafe name, unavailable safe filesystem operation, or unsafe destination still
blocks the skill update and reports that the observation was not recorded.
Valid names remain recordable when external ownership discovery fails. Verify
`tests/plugins/test_canonical_skill_guard.py`. This intake remains a plugin
candidate for removal when the canonical profile source-guard replaces it across
every supported consumer. Reverting restores the older unsafe observation writer.

## Current ownership

The six authored profile plugins are maintained in the separate `agents`
repository under `agents/sources/plugins/`:

- `goal-lifecycle`
- `memory-journal`
- `output-guard`
- `request-update`
- `review-candidate`
- `source-guard`

They remain Hermes-native projections. Their source, tests, ownership metadata,
and target build belong to `agents`, not to this Hermes application checkout.
The 2026-09-26 audit verified all six installed profile projections against their
canonical sources. Source parity does not establish live hook acceptance.

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
plugin tests from `agents/sources/plugins/` with the Hermes runtime on the test path.
Verify the installed profile projection separately. A source test or successful
projection build does not establish live gateway acceptance.

## Retirement and rollback

The old in-checkout source tree and its source-local installer/tests are retired.
Rollback of this ownership transfer means restoring the previous source commit
and installer only through an explicitly reviewed change. Do not restore a
second source authority as an operational shortcut.

## Remaining Retirement: Canonical-Skill-Guard

The patch identity `canonical-skill-guard` owns `plugins/canonical-skill-guard/`.
It keeps externally owned canonical skills read-only. The replacement
profile-owned `source-guard` now lives in `agents/sources/plugins/source-guard/`.
The 2026-09-26 profile has the replacement enabled and the bundled guard
disabled. That configuration is not retirement or proof of live acceptance.
Keep changes here narrow until the replacement is qualified. Verify with
`tests/plugins/test_canonical_skill_guard.py` and
`hermes plugins validate plugins/canonical-skill-guard`. Retire this identity by
deleting the plugin once the merged profile guard is installed, enabled, and
verified live.

## Native Memory Transactions

Fork patch identity: `memory-transaction-observers`. The native MemoryStore emits
profile-scoped before/after transaction snapshots while holding its target file
lock. Optional observers can prepare durable receipts before the write and
complete them after it. Preparation failures prevent mutation. Native
compare-and-restore checks exact expected bytes under that same lock. Neither
operation changes the frozen prompt snapshot or exposes snapshots to model tools.

The optional journal remains in agents. Version 0.2 requires this API, so install
it only after the managed runtime includes the core change. Legacy snapshots and
ambiguous commit receipts cannot authorize undo. Upstream contract discussion:
[#105397](https://github.com/NousResearch/hermes-agent/issues/105397).
Retire the local primitive when an accepted upstream version supplies equivalent
locked transaction observation and compare-and-restore semantics.

Verification: `scripts/run_tests.sh tests/tools/test_memory_transactions.py
tests/tools/test_memory_tool.py`. Roll back core and its dependent journal version
together. Preserve journal history and memory files across code rollback.
