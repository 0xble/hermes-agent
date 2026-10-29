# Bounded filename search

## Required behavior

`search_files(target="files")` runs `rg --files` with a small explicit walker-thread
bound in both the native and shell execution lanes. The bound is derived from the
host CPU count as `max(1, min(4, cpu_count // 4))`, so a large host cannot turn one
filename search into a whole-machine parallel walk. An unknown CPU count uses one
thread. Content searches keep their existing ripgrep command and parallelism.

Filename globs retain their complete path and brace syntax in both ripgrep
transports. Slash-bearing patterns match the complete suffix at any depth within
the selected roots, while ordinary basename globs keep their existing matching.
The historical basename reduction belongs only to the `find -name` fallback,
whose parser does not support ripgrep's path and brace syntax. That fallback was
the original reason for the reduction (`533c064269417`), not a requirement to
truncate globs sent to ripgrep.

Filename-search failures include ripgrep's exit status and at most 4096 bytes of
engine stderr in the structured `error`, never in the returned paths. Native
execution drains stderr separately even after reaching the capture bound. Shell
execution separates diagnostics from paths without creating backend temp files.
Admission locks, root selection, traversal bounds, pagination, ignore rules, and
hidden-directory defaults remain unchanged. Complex globs do not use the
basename-only directory supplement, which cannot interpret their syntax.

## Provenance and patch

Fork patch identity: `bounded-filename-search`.

Maintenance evidence from the Mac Studio on 2026-10-11 showed filename searches over
`$HERMES_HOME` and `cache/scratch` spawning `rg --files` at roughly 900–1000% CPU.
The filename lane already bounded result admission and timeout, but its ripgrep
walker remained unbounded. This patch changes only the filename command builder and
its regression coverage.

Upstream guidance was checked at `835c3dd038d903182a66ecc45e34bf5e0de65f00`
and rechecked at `41cd7b649d0980c57620035af736f91be10dae79` during recovery;
no relevant contract change was found.
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
native and shell transports. The base fails 10 cases and passes 6 unchanged
basename/alternative cases. The corrected implementation passes all 16 cases.

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
