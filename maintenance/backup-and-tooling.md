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
  `evidence` (records, not patches). `maintenance-contract` is owned
  by the root contract.
- `HERMES-123` (`0cac0f8432`) stops `_run_full_backup` reporting a held backup slot as a
  failed backup. Only the `full` pre-update mode reaches it; `quick` (this install's
  setting) has its own message on the snapshot path. Retire it if the two stop sharing
  one cross-process slot, which is the fix the message exists to compensate for.
- Upstream contribution: none recorded for the local patches. The two adopted backup
  fixes retire when the candidate release retains them.

## Verification

`scripts/run_tests.sh` on `tests/hermes_cli/test_backup.py`,
`tests/hermes_cli/test_backup_stability.py`, `tests/scripts/test_candidate_scripts.py`,
`tests/scripts/test_fork_patch_trailers.py`, `tests/cron/test_per_job_timezone.py`,
`tests/cron/test_cron_timezone_migration_catchup.py`,
`tests/cron/test_contention_skip_observability.py`, `tests/agent/test_model_metadata.py`,
and `tests/agent/test_context_compressor.py`. Exercise rollback failure recovery and the
native update receipt check before promotion.

## Retirement and rollback

Retire snapshot integrity when upstream verifies quick-snapshot recovery copies; per-job
timezone when upstream adds one; contention observability when upstream records skips.
Schema rehearsal and the legacy archive are migration tooling with no retirement. Roll back
source by reverting the logical patch; installed runtime rollback follows the runtime owner.
