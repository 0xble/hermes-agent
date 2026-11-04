# Bounded filename search

Fork patch identity: `bounded-filename-search`.
Contract tests: `tests/tools/test_search_filename_globs.py`, `tests/tools/test_search_cwd_preservation.py`, `tests/tools/test_search_files_engine_selection.py`, `tests/tools/test_search_budget_truncation.py`, `tests/tools/test_macos_protected_search.py`.

## Current contract

Filename matching has three invariants: changing the terminal/backend cwd,
spelling the same root relatively or absolutely, or adding an unrelated root
cannot change matches inside an existing root. An extra root contributes only
its own matches. Results are absolute paths, deduplicated in caller root order
for discovery; modified order remains globally newest-first.

| Pattern | Match within each selected root |
| --- | --- |
| Unanchored basename without `/` or braces | Basename at any depth, preserving the historical implicit leading `*` |
| Unanchored slash/brace glob, including explicit `**/` | Root-relative path suffix at any depth (`**/<pattern>`); also accept the variant with a repeated literal root path or root basename stripped |
| Leading `./` or `/` | Root-relative path without an implicit recursive prefix |
| Absolute path inside a selected root | Root-relative path in that root only, preserving absolute root identity |

Each root runs its own bounded `rg --files` job in an isolated subprocess cwd
(or shell subshell). A literal root prefix in that root's parent frame also
supports file operands without attempting to cd into a file. No shared ancestor
or caller-cwd matching coordinates are used. POSIX roots require an absolute
backend cwd; expand `~` using the backend HOME, never the controller's. Unsupported
coordinates fail explicitly rather than returning silent empty matches.

Filename walkers use `max(1, min(4, cpu_count // 4))` threads (one if CPU count
is unknown). Root admission, bounded result capture, pagination and the shared
60-second rg deadline remain in force. Discovery stops at the page budget;
modified order merges each root's bounded newest candidates globally. Native
and shell transports keep bounded stderr separate from paths and report engine
status plus at most 4096 diagnostic bytes on failure. Neither transport changes
the caller's session cwd, including after empty matches or failures.

Ripgrep ignore rules, hidden-directory defaults and macOS protected-root pruning
remain enabled. Basename globs (including `**/basename`) retain the `find`
directory supplement in every root so empty directories remain discoverable;
path/brace globs exceed its basename syntax. The no-rg `find -name` fallback
retains its historical basename-only syntax. Content search is unchanged.

## Verification

Run in an invocation-owned sandbox home with a prepared test interpreter;
`scripts/run_tests.sh` acquires machine-wide heavy-slot admission:

```sh
scripts/run_tests.sh tests/tools/test_search*.py tests/tools/test_file_operations*.py tests/tools/test_macos_protected_search.py --file-retries 0
```
