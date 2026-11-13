# Restorable scratch prune

**Patch identity:** `scratch-prune-restorable`.

## Required divergence

The 24-hour scratch prune must not make an irreversible delete. After process and worktree
reaping, an expired top-level entry is moved with same-volume `os.rename` into
`<realpath(scratch).parent>/.scratch-trash/<UTC-date>/entries/<relative path>`. For a
non-symlinked scratch this is `<home>/cache/.scratch-trash`; for a symlink onto another volume,
it stays beside the target rather than crossing volumes from the configured home. As a sibling,
the holding root cannot be selected as a normal scratch entry. The holding root, day directories,
and `entries/` directories follow `apply_secure_dir_policy` using the configured owning home,
including existing directories from earlier passes. A JSONL manifest at
`<UTC-date>/manifest.jsonl` records the original path, apparent tree size, source mtime, prune
timestamp, and holding path. Entries live under `entries/` so no scratch entry name can collide
with the manifest. A later pass purges held entries older than seven days and appends a `purged`
record; it never rewrites or deletes the manifest. A failed manifest read leaves both the
manifest and that day's held entries untouched. Manifests remain as append-only audit history even
after all their entries are purged. Holding-dir cleanup uses an explicit bottom-up stack (Python
3.11's `os.walk` is recursive) and `lstat` before traversing each directory: symlinks and junctions
are unlinked as links and never traversed. A held subtree that cannot be removed is tracked with
the existing stuck-entry state outside `entries/` and is not retried every pass.

Existing `.keep` and transitive `input-pins.json` retention is unchanged. Enabled cron jobs are
also scanned using the Fleet generator's PR #691 field set and token matcher (`prompt`, `script`,
`script_args`, `args`, `command`); a literal scratch path keeps its top-level entry. Matching
uses raw string values (including strings nested in argv lists and mappings), not JSON-encoded
text, so Windows backslash prefixes are preserved. Disabled
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
  `tests/test_scratch_dir.py::test_holding_dir_resolves_beside_symlink_target_outside_scratch`
  `tests/test_scratch_dir.py::test_prune_symlinked_scratch_avoids_cross_device_rename`
  `tests/test_scratch_dir.py::test_holding_directories_follow_scratch_permission_policy`
  `tests/test_scratch_dir.py::test_job_references_match_raw_windows_paths`
  `tests/test_scratch_dir.py::test_postremoval_unreadable_entry_is_not_counted_as_removed`
  `tests/test_scratch_dir.py::test_prune_keeps_enabled_job_references_but_not_disabled_jobs`
  `tests/test_scratch_dir.py::test_prune_moves_symlink_without_following_target`
  `tests/test_scratch_dir.py::test_entry_named_manifest_is_held_intact_and_restorable`
  `tests/test_scratch_dir.py::test_failed_manifest_read_does_not_delete_manifest`
  `tests/test_scratch_dir.py::test_purge_preserves_a_hold_appended_after_manifest_read`
  `tests/test_scratch_dir.py::test_purge_held_tree_does_not_follow_directory_links`
  `tests/test_scratch_prune_undeletable.py::test_undeletable_or_unreadable_entry_is_moved_without_walking_contents`
  `tests/test_scratch_prune_undeletable.py::test_unreadable_held_subtree_is_tracked_without_aborting_pass`
  `tests/test_scratch_prune_undeletable.py::test_deep_held_tree_is_removed_without_recursion_error`
  `tests/test_scratch_prune_undeletable.py::test_recursion_error_during_held_removal_does_not_abort_pass`

## Retirement

Retire when upstream provides equivalent restorable scratch pruning, holding purge, and cron
reference retention, and the focused tests pass without this patch. Roll back by reverting the
commit carrying `Fork-Patch: scratch-prune-restorable`.

Fork-Patch: scratch-prune-restorable.
