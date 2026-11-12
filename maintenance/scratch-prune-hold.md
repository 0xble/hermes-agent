# Restorable scratch prune

**Patch identity:** `scratch-prune-restorable`.

## Required divergence

The 24-hour scratch prune must not make an irreversible delete. After process and worktree
reaping, an expired top-level entry is moved with same-volume `os.rename` into
`<home>/cache/.scratch-trash/<UTC-date>/<relative path>`. A JSONL manifest line records the
original path, apparent tree size, source mtime, prune timestamp, and holding path. A later pass
purges held entries older than seven days. Holding-dir cleanup is no-follow: symlinks and
junctions are unlinked as links and never traversed.

Existing `.keep` and transitive `input-pins.json` retention is unchanged. Enabled cron jobs are
also scanned using the Fleet generator's PR #691 field set and token matcher (`prompt`, `script`,
`script_args`, `args`, `command`); a literal scratch path keeps its top-level entry. Disabled
jobs do not retain inputs. A missing jobs file retains nothing; malformed or unreadable jobs data
fails closed for that pass.

## Provenance and adoption

This is a fork-only safety patch for the live incident where an approved cron input was deleted
by the 24-hour prune. The selected route reuses Fleet's already-published matching behavior from
`0xble/scripts#691`, rather than requiring a generator run during pruning. The first promotion
after the 2026-11-13 switch cutover is the activation point; the switch freeze's scratch-prune
hold covers the interval before promotion.

Preserve the same-volume rename boundary, no-follow behavior, seven-day purge, manifest fields,
existing pins, enabled-job matching, and fail-closed handling when syncing upstream. Restore is
manual: move a held entry back to its recorded original path before the seven-day purge.

Contract tests: `tests/test_scratch_dir.py::test_prune_moves_expired_entry_to_restorable_holding_dir`
  `tests/test_scratch_dir.py::test_prune_purges_holding_entries_after_seven_days`
  `tests/test_scratch_dir.py::test_prune_keeps_enabled_job_references_but_not_disabled_jobs`
  `tests/test_scratch_dir.py::test_prune_moves_symlink_without_following_target`
  `tests/test_scratch_prune_undeletable.py::test_undeletable_or_unreadable_entry_is_moved_without_walking_contents`

## Retirement

Retire when upstream provides equivalent restorable scratch pruning, holding purge, and cron
reference retention, and the focused tests pass without this patch. Roll back by reverting the
commit carrying `Fork-Patch: scratch-prune-restorable`.

Fork-Patch: scratch-prune-restorable.
