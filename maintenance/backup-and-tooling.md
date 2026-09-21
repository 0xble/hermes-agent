# Backup, state, and fork maintenance tooling

Load this unit when changing backup retention or verification, schema rehearsal, the
candidate sync/check/rollback scripts, or the pre-contract context ports.

## Required behavior

- Incomplete backup archives are reported as failures; complete archives survive
  retention; SQLite snapshot members are verified before a quick snapshot is trusted.
- `scripts/schema_rehearsal.py` proves a copied legacy database opens, migrates, and keeps
  row counts, search results, and fork-only rows. A successful open alone is insufficient.
- `scripts/sync_fork_candidate.py` builds candidates only; `scripts/check_fork_patches.py`
  proves trailers, unit ownership, extension registration, config keys, and the native
  update receipt; `scripts/rollback_fork_runtime.sh` reaches recovery when reinstall fails.
  Scheduled copies live under `$HERMES_HOME/scripts` per [runtime ownership](runtime-ownership.md).
- Cron: per-job IANA `job_timezone` with civil-time scheduling and a migration dry-run
  diff; contention skips are persisted truthfully. Fallback routing is owned by
  [cron fallback routing](cron-fallback-routing.md).
- Context: `compression.threshold_tokens` defaults to 256K, and `codex_responses` custom
  endpoints resolve the Codex OAuth window (fork PR #4, before the trailer floor; upstream
  landed the resolver fix via #116329).

## Provenance and patches

- Fork patch identities: `slice-13-incomplete-archive-status` (upstream `ccb3d968ced`),
  `slice-13-bounded-retention` (upstream `1250a3e4eb6`), `slice-13-snapshot-integrity`,
  `slice-13-schema-rehearsal`, `slice-14-self-update`, `slice-18-archive`,
  `slice-12-per-job-timezone`, `slice-12 truthful-contention` (the space is the trailer's
  literal identity; do not normalize it or `f500063ab41a` becomes unowned),
  `maintenance-tooling`, `update-lifecycle`, `trailer-floor`, `HERMES-123`,
  `backup-zip-timestamps`, `evidence` (records, not patches). `maintenance-contract` is owned
  by the root contract.
- `HERMES-123` (`0cac0f8432`) stops `_run_full_backup` reporting a held backup slot as a
  failed backup. Only the `full` pre-update mode reaches it; `quick` (this install's
  setting) has its own message on the snapshot path. Retire it if the two stop sharing
  one cross-process slot, which is the fix the message exists to compensate for.
- Upstream contribution: none recorded for the local patches. The two adopted backup
  fixes retire when the candidate release retains them.
- `backup-zip-timestamps`: both full ZIP writers use the standard library's timestamp
  clamping so pre-1980 and post-2107 files remain recoverable. Source timestamps,
  content, selection, failure handling, and pruning are unchanged. Narrow adaptation
  of the timestamp portion of upstream PR #106011 (head `8e3f3d7b757c48dc0cefc301f751c2da6da354e0`),
  tracking issue #105868. The broader PR's selection/reporting/pruning changes are
  intentionally excluded. Proof: `test_zip_timestamp_bounds_preserve_files` in
  `tests/hermes_cli/test_backup.py` exercises manual internal/external files and
  automatic home-only archives with real files and ZIP readback. Retire when the
  selected upstream release passes this contract. Rollback only this logical patch,
  not adjacent backup safety fixes, through the runtime owner's supported update path.

## Verification

Published commit `e5d121dfd63d` omitted its trailer during squash merge. Its
desktop fixture portability fix and local CI declaration are owned by
`maintenance-tooling`. This exact stable patch ID backfills only that content,
including after a release rebase. It does not advance the trailer floor.

Fork-Patch-Backfill: f0a3bb8be8b611d30a990bb83db46aeaed37c39a; maintenance-tooling

`scripts/run_tests.sh` on `tests/hermes_cli/test_backup.py`,
`tests/hermes_cli/test_backup_stability.py`, `tests/scripts/test_candidate_scripts.py`,
`tests/scripts/test_fork_patch_trailers.py`, `tests/cron/test_per_job_timezone.py`,
`tests/cron/test_cron_timezone_migration_catchup.py`,
`tests/cron/test_contention_skip_observability.py`, `tests/agent/test_model_metadata.py`,
and `tests/agent/test_context_compressor.py`. Exercise rollback failure recovery and the
native update receipt check before promotion.

Source sync refreshes fork `main`, selects release tags from upstream only, proves
release ancestry, and runs the canonical isolated runner over maintained proof
surfaces before candidate publication. Source-only ownership verification keeps
unpromoted candidate checks separate from installed update receipts.
`tests/scripts/test_sync_fork_candidate.py` exercises local Git remotes, stale refs,
new releases, candidate-only publication, failure refusal, and worktree recovery.
`tests/plugins/test_candidate_extensions_install.py` verifies maintenance entry
points follow promoted code without changing config. These repairs belong to the
existing `maintenance-tooling` identity and remain local fork automation.

## Retirement and rollback

Retire snapshot integrity when upstream verifies quick-snapshot recovery copies; per-job
timezone when upstream adds one; contention observability when upstream records skips.
Schema rehearsal and the legacy archive are migration tooling with no retirement. Roll back
source by reverting the logical patch; installed runtime rollback follows the runtime owner.
