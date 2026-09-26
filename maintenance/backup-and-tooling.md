# Backup, state, and fork maintenance tooling

Load this unit when changing backup retention or verification, schema rehearsal, the
candidate sync/check/rollback scripts, or the pre-contract context ports.

## Required behavior

- Incomplete backup archives are reported as failures; complete archives survive
  retention; SQLite snapshot members are verified before a quick snapshot is trusted.
- `scripts/schema_rehearsal.py` proves a copied legacy database opens, migrates, and keeps
  canonical row counts, metadata, and fork-only rows. Search changes from the upstream
  FTS v2-to-v3 migration require independent canonical-projection and rank-1 index
  integrity checks. Unexpected differences and query errors fail the rehearsal.
  A successful open alone is insufficient.
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
  `cron-profile-timezone-reanchor`,
  `backup-zip-timestamps`, `vanished-entry-test-contract`, `snapshot-prune-latch`,
  `full-zip-failure-accounting`, `config-backup-content`, `sqlite-backup-wal-snapshot`,
  `evidence` (records, not patches), `candidate-tooling` (candidate sync/check scripts and
  their review fallback), `slice-14-request-update` (the parent-only native update request).
  `maintenance-contract` is owned by the root contract.
- `config-backup-content`: config backup deduplication reads current bytes rather
  than reusing `filecmp` results cached by paths and stat signatures. Same-size
  edits with preserved mtimes must produce a new backup. The existing config
  backup test controls mtimes and checks deduplication and exact retained bytes.
  Upstream main `6c4536aed298112e976cf7c484ee63e99150e09d` still uses the cached
  comparison. Open upstream [PR #110248](https://github.com/NousResearch/hermes-agent/pull/110248)
  addresses separate same-second destination collisions and retains that comparison.
  Open [PR #106871](https://github.com/NousResearch/hermes-agent/pull/106871) changes
  Docker rollback selection, not this helper. Neither replaces this fix.
  Verify with `scripts/run_tests.sh tests/hermes_cli/test_config_backups.py
  tests/hermes_cli/test_config_lkg_backup.py --file-retries 0`.
  Retire when an accepted upstream release passes the controlled-mtime invariant
  without this patch. Rollback only the byte-comparison change, its regression
  additions, and this record, preserving other backup and CI patches.
- `HERMES-123` (`0cac0f8432`) stops `_run_full_backup` reporting a held backup slot as a
  failed backup. Only the `full` pre-update mode reaches it; `quick` (this install's
  setting) has its own message on the snapshot path. Retire it if the two stop sharing
  one cross-process slot, which is the fix the message exists to compensate for.
- `vanished-entry-test-contract`: test-only, on top of the diagnostics rewrite in #54.
  Adds the one assertion #38 never made: `entry_vanished=` is recorded in the profile log.
  #38 is NOT uncovered -- `fc45821e1f` shipped four tests pinning the return value, the
  critical-file exception, the nested-basename case and the mass-vanish ceiling -- but all
  four read the console and the filesystem, and the console preview is capped, so a run
  diagnosed days later has only the log.
  Also renames the fixture's archives to timestamps. `_newest_first` orders by NAME, so
  `previous` sorted above `current` and a regressed prune gate would have deleted the NEW
  archive with the preservation assertion still passing. Both additions are mutation-proven:
  dropping `not errors` from the prune gate, and routing vanished entries back to `on_error`,
  each fail exactly one of them.
  Retire it if the two classifications are ever merged back into one.
- `snapshot-prune-latch`: a file omitted for size is a standing property of that file, so it
  must not latch the snapshot prune off forever. A capture failure still blocks the prune; a
  size skip prunes while protecting any snapshot that still holds a file this run omitted, so
  the only copy is never the one deleted. The same pass reclaims abandoned `.partial` staging
  directories, which `_snapshot_dirs` excludes by design and nothing else ever removed. It runs
  under the caller's exclusive backup lock. The just-published snapshot is excluded from the
  prune because ordering is by name, not mtime, and recycled ids can sort the newest first.
  Retire it if snapshot retention moves to an age-based policy that no longer consults
  per-file omission state.
- `full-zip-failure-accounting`: the pre-update/pre-migration ZIP is a rollback point written
  with no console attached, so the log is its only record. Per-entry failures went to DEBUG,
  below the default level, and the completion line reported the scan's selected count as
  `files=`, so an archive missing data was indistinguishable from a whole one. Failures are now
  named at WARNING, files removed between scan and write are counted apart from read errors as
  routine churn, and the summary reports written, selected, errors and vanished.
  Three further defects surfaced in review and are fixed in the same unit, because the counts
  are worthless if the archive and the retention do not agree with them:
  (a) `_discard_partial_entries` drops the central-directory records of any member whose write
  died
  partway. `ZipFile.write` opens the member and then reads the source into it; the fault is in
  that read, so the destination closes cleanly and registers itself, leaving a TRUNCATED
  member carrying a valid CRC:
  `testzip()` was clean and a restore overwrote the real file with a short one. This is in the
  shared writer and the external-entry loop, so the interactive path gets it too. Keyed on
  position, not name, so a duplicate arcname cannot drop an earlier good member. The
  abandoned bytes are left in place: rewinding `start_dir` to reclaim them yields an archive
  neither zipfile nor `unzip` will open, because nothing truncates the file and the stale
  tail defeats the backward scan for the end-of-central-directory record. That scan covers
  the last 64 KiB, so the mistake is invisible below that and fatal above it, which is
  exactly where the reclaim would have mattered. The regression test therefore needs an
  incompressible payload and a partial read over 64 KiB, or it passes either way.
  (b) An incomplete archive can no longer rotate a whole one out, and cannot accumulate either.
  `_create_prefixed_full_backup` pruned on any non-None return, so one permanently unreadable
  file walked every retained rollback point off the end in `keep` updates. Skipping the prune
  fixes that and replaces it with unbounded growth, since the same unreadable file recurs every
  run: measured 8 archives retained against `keep=5`. An incomplete archive is instead marked
  with an `.incomplete.json` sidecar and pruned against `_MAX_INCOMPLETE_KEPT`, so the two
  classes never compete. Completeness has to be durable because the prune runs in a later
  process that never saw the run.
  (d) Both callers now say so on the console. The claim that a pre-update backup has no console
  attached is wrong: `_run_full_backup` prints a report and `_apply_migration` called
  `print_success` on a partial archive. The counts reach them through an `outcome` dict.
  (c) A mass disappearance now counts here too, and the rule gained an absolute floor.
  `_is_mass_vanish` requires both `_MAX_VANISHED_SHARE_FOR_PRUNE` and
  `_MIN_VANISHED_FOR_MASS`, because churn is an absolute quantity (a few files per run at any
  home size) while coverage loss is relative: the share alone read one routine rotation on an
  11-file home as a mass disappearance, which after (b) costs that install four of its five
  rollback points. A floor can only relax the verdict on churn, never on critical state, which
  `_is_critical_state` routes to `on_error` before the vanished branch is reached.
  (e) `status=` and `coverage=` are separate fields on both log lines. One word carrying both
  facts made the log contradict the console twice, in opposite directions: first always
  `complete`, then `incomplete` while the console said complete on a vanished-only run.
  `status` is the run's outcome and tracks errors, matching the console and the exit code;
  `coverage` is whether the archive still covers what older ones do. The marker and the
  retention cap stay driven by either cause, since those are about retention safety.
  Note the vocabulary this leaves: a mass-vanish-only run logs `status=complete
  coverage=reduced` and still writes an `.incomplete.json` marker and caps retention. The
  words differ on purpose, because the run did succeed and the archive is still restorable
  while covering less than its predecessors. Grepping `status=incomplete` will not find such
  a run; the `incomplete errors=N vanished=N of N` line emitted when the marker is written
  will, and so will `mass_vanish`. Do not "fix" the disagreement by making either word
  follow the other: that is the overload which made the log contradict the console twice.
  The archive is still kept and still returned: a partial rollback point beats none. Whether an
  incomplete one should block an update is a separate decision, unchanged here.
  Retire it if the automatic path adopts the interactive path's structured report.
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
- `sqlite-backup-wal-snapshot`: an incremental online backup of a WAL database pins one
  read snapshot before copying pages, so commits from another connection cannot restart
  a large copy indefinitely. WAL writers remain live; non-WAL databases retain short
  per-step read locks instead of blocking writers for the whole copy. Snapshot setup retries
  only `SQLITE_BUSY`/`SQLITE_LOCKED` within one bounded deadline, preserving transient
  rollback-writer and WAL-recovery availability without hiding other operational errors.
  A pinned WAL reader can delay checkpoint frame reclamation, so the WAL may grow while a
  long snapshot runs; that bounded storage tradeoff prevents unbounded backup restarts.
  Owner-only destination creation, fail-closed cleanup, and copy verification remain
  unchanged. Upstream main `c80d12b9b98e36178aa41d496e8dd555399fc286` still has the
  restartable copy. Upstream issue #86630 and merged PR #86680 bound consecutive
  `SQLITE_BUSY`/`SQLITE_LOCKED` statuses but did not cover successful page copies repeatedly
  restarted by concurrent WAL commits. Retire when a selected upstream release passes the
  concurrent-writer snapshot contract.
- `cron-profile-timezone-reanchor`: an unpinned cron job's future `next_run_at`
  can retain the old profile timezone until the old instant becomes due after a
  profile move, because `get_due_jobs` only repaired shifted offsets at due time.
  Re-anchor future legal civil-time slots during the isolated due scan, comparing
  the stored offset with the governing zone's offset *at that instant*, not the
  scan-time offset (which legitimately differs across DST). Keep overdue and
  already-passed wall-clock slots eligible for the existing catch-up path;
  preserve pinned jobs, manual triggers, and edited-expression handling.
  Related upstream [PR #97491](https://github.com/NousResearch/hermes-agent/pull/97491)
  proposes explicit schedule timezone stamps; it is open, not a released replacement.
  Proof: `scripts/run_tests.sh tests/cron/test_cron_timezone_migration_catchup.py
  tests/cron/test_per_job_timezone.py --file-retries 0` and the full `tests/cron/`
  suite. Retire if a selected upstream release re-anchors future unpinned jobs
  after a profile timezone change without losing due slots. Roll back only
  `_reanchor_future_timezone_shifted_cron`, its due-scan call, the two regression
  tests, and this record; retain the older due-time migration catch-up.

## Verification

Published commit `e5d121dfd63d` omitted its trailer during squash merge. Its
desktop fixture portability fix and local CI declaration are owned by
`maintenance-tooling`. This exact stable patch ID backfills only that content,
including after a release rebase. It does not advance the trailer floor.

Fork-Patch-Backfill: f0a3bb8be8b611d30a990bb83db46aeaed37c39a; maintenance-tooling

`scripts/run_tests.sh` on `tests/hermes_cli/test_backup.py`,
`tests/hermes_cli/test_backup_stability.py`, `tests/scripts/test_candidate_scripts.py`,
`tests/scripts/test_schema_rehearsal.py`,
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
The maintenance-only compatibility installer verifies entry points follow promoted
code without changing config; it preserves flat paths for regular directories
and writes under `hermes/` for a linked scripts root. The linked-layout check
uses a disposable profile; actual cron path edits remain a separate cutover.
These repairs belong to the existing
`maintenance-tooling` identity and remain local fork automation.

## Retirement and rollback

Retire snapshot integrity when upstream verifies quick-snapshot recovery copies; per-job
timezone when upstream adds one; contention observability when upstream records skips.
Schema rehearsal and the legacy archive are migration tooling with no retirement. Roll back
source by reverting the logical patch; installed runtime rollback follows the runtime owner.
