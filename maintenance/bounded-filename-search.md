# Bounded filename search

## Required behavior

`search_files(target="files")` runs `rg --files` with a small explicit walker-thread
bound in both the native and shell execution lanes. The bound is derived from the
host CPU count as `max(1, min(4, cpu_count // 4))`, so a large host cannot turn one
filename search into a whole-machine parallel walk. An unknown CPU count uses one
thread. Content searches keep their existing ripgrep command and parallelism.

Filename globs retain their complete path and brace syntax in both ripgrep
transports. Unanchored patterns retain the base ripgrep invocation's cwd and root
operands: no synthesized `**/`, root rebasing, or common-parent cwd. The matching
contract below applies identically for one or several roots; adding an unrelated
root cannot change matches under an existing root.

| Pattern kind | Root kind | String matched by the glob |
| --- | --- | --- |
| Basename (no `/`) | Relative or absolute | Basename at any depth; retain the historical leading `*` unless already present |
| Unanchored path (including braces and explicit `**/`) | Relative | Cwd-relative operand spelling plus descendant path (retain leading `../`; omit `./`) |
| Unanchored path (including braces and explicit `**/`) | Absolute below cwd | Cwd-relative file path, as ripgrep strips its cwd prefix before glob matching |
| Unanchored path (including braces and explicit `**/`) | Absolute outside cwd | Absolute file path |
| Leading `./` or `/` | Relative or absolute | Descendant path relative to each selected root; no implicit recursive prefix |
| Absolute pattern inside a selected root | Relative or absolute | Descendant path relative to that root only (preserve root identity) |

Ripgrep still emits absolute paths for absolute operands, including those below
cwd: emitted paths are not always its glob-match strings. Slash-bearing globs
are passed verbatim, so `deep/*` is not shorthand for `**/deep/*`. Other leading
`/` patterns are root-relative syntax. Only root anchoring needs a shared parent
cwd and literal root-prefix escaping, keeping file operands out of `cd` targets.
The base's existing macOS protected-multi-root exclusion scope is retained: that
special traversal runs from the roots' common ancestor in both base and head.
Each search remains one bounded ripgrep invocation, preserving global modified
order and pagination. Non-POSIX roots/cwd retain the base unanchored path;
anchored globs fail explicitly rather than guessing POSIX coordinates.
The historical basename reduction belongs only to the `find -name` fallback,
whose parser does not support ripgrep's path and brace syntax. That fallback was
the original reason for the reduction (`533c064269417`), not a requirement to
truncate globs sent to ripgrep.

Filename-search failures include ripgrep's exit status and at most 4096 bytes of
engine stderr in the structured `error`, never in the returned paths. Native
execution drains stderr separately even after reaching the capture bound. Shell
execution separates diagnostics from paths without creating backend temp files.
Admission locks, root selection, traversal bounds, pagination, ignore rules, and
hidden-directory defaults remain unchanged. Basename globs, including paths
whose directory components are only `**`, retain the directory supplement so
empty and nested directories remain discoverable. Other path globs and brace
alternation exceed that supplement's basename-only syntax. Shell status
propagation lets the environment wrapper finish capturing status, saving its
snapshot, and reporting the observed working directory. The shared session cwd
must stay at the caller's directory after successful, empty, or failed searches.
`_run_rg_bounded` scopes its shell transport's explicit cwd in `(cd -- ... && ... )`,
not in the wrapper shell. The native transport scopes only its subprocess cwd.
This protects the directory supplement within a call as well as subsequent
relative search/read operations. Expand a tilde cwd using the backend's HOME
before root anchoring; never use the controller's home. If a cwd cannot be
resolved to an absolute POSIX path, anchored searches fail explicitly rather
than guessing. Unanchored searches never translate their original operands.

Cwd-mutation audit: `file_operations_search.py`'s filename rg builder was the only
bare `cd` command. The shared bounded-rg transport now owns its isolation. The
content rg/grep builders, pruned grep pipeline, find filename fallback,
directory-supplement find, zero-match hint `ls`, and `file_operations.py`'s read,
read-probe, byte-exact/hex, Python-snippet, atomic-write, patch, move/delete, and
not-found suggestion `ls` builders contain no directory-changing shell command.
`file_operations_common.py`, `file_operations_native.py`, and
`file_operations_lint.py` contain no directory-changing shell commands either.

## Provenance and patch

Fork patch identity: `bounded-filename-search`.

Maintenance evidence from the Mac Studio on 2026-10-11 showed filename searches over
`$HERMES_HOME` and `cache/scratch` spawning `rg --files` at roughly 900–1000% CPU.
The filename lane already bounded result admission and timeout, but its ripgrep
walker remained unbounded. This patch changes only the filename command builder and
its regression coverage.

Upstream guidance was checked at `835c3dd038d903182a66ecc45e34bf5e0de65f00`
and rechecked at `41cd7b649d0980c57620035af736f91be10dae79` during recovery;
no relevant contract change was found. The upstream agent, tool, and contribution
guidance was checked again at `9302ba9e212c63ba24cdf1f47f26b47bcd3b042e` for the
multi-root anchoring correction; no relevant guidance change was found.
Related issue [NousResearch #127861](https://github.com/NousResearch/hermes-agent/issues/127861)
and open PR [#129710](https://github.com/NousResearch/hermes-agent/pull/129710)
propose indexed lookup and completion changes; they are broader alternatives, not
an adopted thread-budget fix. Delivery is an own-fork patch and a draft PR on
`0xble/hermes-agent`; no upstream contribution is authorized for this task.

Broad-root pruning of `cache/scratch` is intentionally not included. An explicit
scratch root must remain searchable, and the implicit-parent distinction needs a
separate path-policy regression surface before it is safe to add. Separate callers
in `hermes_cli/commands_completion.py::_get_project_files` and
`agent/context_references.py::_iter_visible_entries` also issue `rg --files`; they
are follow-ups outside the requested agent-tool command path. The read-file
not-found suggestions use a same-directory `ls -1 ... | head -50`, not a recursive
ripgrep walk.

## Verification

The RED test on the base asserts that a 28-CPU host's filename command contains
`--threads 4` and fails because the command has no thread bound. The GREEN focused
suite is:

`scripts/run_tests.sh tests/tools/test_search_files_engine_selection.py tests/tools/test_search_native_rg.py tests/tools/test_search_files_cpu_windows.py tests/tools/test_search_zero_match_and_multipath.py --file-retries 0`

The filename-glob regression suite is
`tests/tools/test_search_filename_globs.py`. It exercises the exact hook-config
brace union, basename and path globs, hidden-directory pruning, malformed-glob
status and diagnostics, and bounded draining of a noisy stderr stream in both
native and shell transports. It also covers recursive directory matches,
relative and root-anchored prefixes, and the shell wrapper's observed working
directory after a failed search. On 2026-10-29, the expanded suite against a
detached checkout at `1f1d80a42a69` produced 20 failed and 14 passed cases. The
corrected implementation passes all 34 cases.

The multi-root anchoring regression at `46230b4e63f9` produces 50 failures and
34 deselections before the correction: root-anchored and absolute patterns, both
ordering modes, relative/absolute roots, roots outside the working directory, and
literal metacharacters in root names. Both shell and native transports are
exercised, and native execution is asserted rather than silently falling back.
The corrected filename-glob module passes all 84 cases. The expanded GREEN run
through `heavy-slot` and `scripts/run_tests.sh` passes 452 tests across 17 search
and file-operations modules with 0 failures and 12 platform skips.

The round-3 coordinate matrix uses a pure-Python per-root walk oracle and real
ripgrep in both transports. Its 192 cells cover root count (1/2), root location
(cwd/subdirectory/outside), spelling (relative/absolute), and eight pattern forms:
`/x`, `/sub/*`, absolute inside/outside paths, basename, `**/dir/x`, brace union,
and `./x`. It asserts exact normalized path sets and native execution, with 12
additional cases for the reviewer's six single-root reproductions and two
non-POSIX fail-before-execution cases. Against `6cc7b62c1893`, the new cases give
60 failed, 146 passed, and 84 deselected. Of these, the matrix alone gives 48
failed and 144 passed. The failures establish missing single-root coordinates
and the previously recursive `./` interpretation. GREEN through `heavy-slot` and
`scripts/run_tests.sh` passes 669 tests across 20 files with 0 failures and 13
platform skips; the filename-glob module passes all 294 cases, including four
file-operand checks. The directory supplement's results are normalized into the
same output coordinates as ripgrep before deduplication. The later compatibility
correction below supersedes this universal-coordinate experiment.

The cwd-preservation regression module `tests/tools/test_search_cwd_preservation.py`
first ran at `0ed0708b9304` and produced **9 failed / 5 passed**: all three shell
variants moved cwd or lost the relative directory supplement; unresolved cwd
coordinates either scoped to a literal tilde or raised mixed-root `commonpath`.
The corrected 18-case module keeps one environment across repeated relative
filename/content searches and read/missing-read operations (no cwd repair between
calls), compares shell/native directory results, checks diagnostic failures, and
covers backend-expanded tilde cwd with mixed absolute/relative roots. The full
21-module search/file-operation run passes **698 tests, 0 failures, 13 platform
skips**, using `scripts/run_tests.sh --file-retries 0` under `heavy-slot` with
per-command sandbox homes and an explicit scratch test interpreter.

Also run the relevant broader tools tests through the canonical runner.
Recovery verification through machine-wide `heavy-slot` admission observed:

- RED: `1 failed, 53 deselected in 0.84s`; the generated filename command
  lacked `--threads 4`.
- GREEN: `15 files, 354 tests passed, 0 failed, 12 skipped` across all nine
  search-engine test modules plus file operations, edge cases, file tools,
  single-roundtrip behavior, cwd resolution, and macOS protected search.
- The native-ripgrep module passed all six tests, including real native and
  shell execution with the thread bound. Windows-only tests were skipped on
  macOS; native Windows qualification remains separate.

## Retirement and rollback

Retire when a selected upstream release bounds `rg --files` walker threads while
preserving the native/shell behavior, complete filename globs, bounded separate
engine diagnostics, and the content-search contract. Roll back the filename
transport and command changes, their regression tests, this maintenance unit,
and its root index row together. The scratch-pruning follow-up can be evaluated independently.
