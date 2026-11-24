# code-health-batch-blobs

Fork patch identity: `code-health-batch-blobs`.

## Problem
In blob:none partial clones (remote.origin.partialclonefilter=blob:none), `scripts/code_health/gitio.py:read_file` (and callers in measure.py, cli.py) called `git show <tree>:<path>` once per file. Each missing blob triggered a separate lazy fetch, creating one tiny promisor pack per file. CI gates (`./bin/ci gate`, `scripts/code_health --base <sha> --head HEAD`) over many files + concurrent worktrees sharing .git/objects/pack grew to 10k+ packs, high fd count, fseventsd load.

## Fix
- Added `_is_partial_clone`, `_ensure_blobs`: for partial clones only, use one `git ls-tree -r -z <tree> -- <paths>` to list oids, one `git cat-file --batch-check` to find missing, one `git fetch origin --filter=blob:none --stdin` (with negotiation noop) to prefetch in batch. Non-partial and tree=None (worktree) unchanged.
- Replaced per-file logic with `read_files` using single `git cat-file --batch` process (fed all `<tree>:<path>` specs), parses headers+bytes, handles missing/binary/utf-8-sig exactly as before. `read_file` delegates to it (signature preserved).
- Updated `measure.py` to call `read_files` once per tree.
- Added focused tests in `tests/scripts/test_code_health.py`: real temp git repo (BOM, binary, missing) proving batch == old per-file results; monkeypatch proving O(1) subprocess per batch (not per file).
- Removed broad-except waiver; fixed ls-tree oid parsing (now filters blob type, correct field index).

## Verification
- `python3 scripts/check --only health` passes with identical results (0 blocking).
- ruff + py_compile clean on touched files.
- All 41 tests in `tests/scripts/test_code_health.py` pass (including new batch tests).
- In non-partial: 1 cat-file subprocess for N files.
- Measurements (before/after on this partial clone): per-gate subprocesses for file reads: N → 1 (or <=3 with prefetch); new promisor packs per gate: N → 1 (or 0 if already present).

## Callers updated
Only internal; public `read_file(repo, tree, path)` unchanged. `tracked_files` etc untouched.

## Related
See `scripts/code_health/gitio.py`, `measure.py`, `tests/scripts/test_code_health.py`, and `scripts/check_fork_patches.py --repo . --source-only` validation.

Contract tests: `tests/scripts/test_code_health.py::test_read_files_batch_equals_old_git_show_and_handles_special_cases` `tests/scripts/test_code_health.py::test_read_files_spawns_one_subprocess_per_batch_not_per_file`
