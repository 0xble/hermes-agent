# Worktree GC Git isolation

Load this unit when changing worktree-GC dirty checks, untracked-file archiving,
reclaim verdicts, or the Git invocation in `hermes_cli/worktree_gc.py`.

## Required behavior

- Production worktree-GC Git calls ignore host configuration: global config is
  `os.devnull`, system config is disabled, and `core.excludesFile` is empty. A
  user's global ignore rules must never hide an untracked file from the safety
  check. Repository-local `.git/info/exclude` and `.gitignore` still apply to
  classification, and ignored-but-present files are listed with
  `--ignored=matching`.
- Untracked and ignored files are listed individually
  (`--untracked-files=all`) and parsed from NUL-delimited output, so a quoted,
  non-ASCII, spaced, or newline-containing path is archived byte-exactly. Rename
  entries keep their extra path field.
- Reclaim is fail-safe: if any listed path cannot be found or copied into the
  archive, the worktree is kept rather than removed.

## Provenance and patches

- Fork patch identity: `worktree-gc-git-isolation`.
- Fork CI already isolates the real-Git test fixtures under
  `fork-ci-reliability` (see [Fork CI](fork-ci.md)). This unit covers the
  production path those fixtures exercise.
- Upstream: own [PR #124019](https://github.com/NousResearch/hermes-agent/pull/124019)
  carries the same change, head `b25ea8f616834f1d2cb97cf48e23762ed215c8f0` on
  2026-09-28. Its first production revision listed individual files without NUL
  parsing; review reproduced silent loss of a quoted filename followed by a
  full reclaim. That revision was never adopted here.
- Surfaces: `hermes_cli/worktree_gc.py`, `tests/hermes_cli/test_worktree_gc.py`.

## Verification

`scripts/run_tests.sh tests/hermes_cli/test_worktree_gc.py`. The regressions
use real Git worktrees: a hostile global `excludesFile` must yield an archiving
verdict, a mixed ordinary and quoted-name untracked directory must archive
every file, and a listed path that cannot be archived must keep the worktree.

## Retirement and rollback

Retire when a released upstream tag isolates the production GC Git calls from
host config and archives NUL-parsed paths with fail-safe reclaim, with
equivalent real-Git coverage. Roll back by reverting the logical patch and this
unit together. No persistent state changes.
