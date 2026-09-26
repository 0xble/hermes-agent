# Verified bounded quick-snapshot recovery

Fork patch identity: `quick-snapshot-recovery`.

## Required behavior

Automatic pre-update snapshots and manually labeled snapshots retain independent windows. A failed or oversized capture must not halt pruning indefinitely or replace the last verified copy of a missing database. A manifest-listed payload counts as recovery only when it is present, its size agrees with the manifest, and SQLite integrity passes for databases. The just-published generation is never deleted by its own prune. A usable previously captured oversized non-database file remains protected too.

## Provenance and adoption

- Archived HERMES-131, `0xble/hermes-agent-archived` source proposal; the narrow current implementation follows [upstream PR #106101](https://github.com/NousResearch/hermes-agent/pull/106101) at `bf9c28bc2ea0c31175d4c049d8d706f53b8174dd`, following issue #106087. Source PR #97768 remains an upstream predecessor for the broader retention model.
- Fork base `c8e3342595ea5021c920ba707e70926d20f1f9f2` already bounds repeated size exclusions but skips all pruning after a failed DB capture. This adaptation shares the upstream verified-recovery algorithm, preserves the fork's abandoned staging cleanup and full-backup session exclusions, includes non-DB oversized recovery already promised by `snapshot-prune-latch`, and keeps publication's explicit newest-generation guard.
- The 1 GiB quick-snapshot cap is intentional; no promise is made to capture a database above it. Do not remove existing snapshots when adopting a later upstream release without first proving the equivalent retention contract on that release.

## Verification and retirement

Run `scripts/run_tests.sh -j 6` on `tests/hermes_cli/test_quick_snapshot_retention.py`, `test_backup_stability.py`, `test_backup.py`, `test_backup_all_profiles.py`, `test_backup_path_errors.py`, and `tests/agent/test_curator_backup.py`. These tests use disposable homes and real SQLite files. Manifest size is not a content digest: a different valid SQLite file of identical size cannot be identified under the present format.

Retire the fork patch only when a selected released upstream revision preserves bounded incomplete retention, verified database and oversized non-DB recovery copies, manual/pre-update family isolation, and just-published generation safety. Roll back this patch through source revert, not by removing existing recovery directories; source rollback cannot resurrect snapshots already expired under retention.
