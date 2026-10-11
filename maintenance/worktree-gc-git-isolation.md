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
- Archiving preserves symlinks, both top-level listed links and links inside
  listed directories, including symlinked directories and dangling links, and
  never follows a link target out of the worktree.
- Reclaim is fail-safe: if any listed path cannot be found or copied into the
  archive, the worktree is kept rather than removed.
- Regenerable directories are removed with the worktree instead of archived.
  Any untracked or ignored path under `.venv`, `node_modules`, `__pycache__`,
  `.pytest_cache`, `.pytest-cache`, `.ruff_cache`, `.mypy_cache` or `.direnv` is
  skipped. `.ci`, `build`, `dist`, `target`, `.next` and `.turbo` are skipped
  only when an ignore rule matches the directory itself (a single `!! dir/`
  entry): an untracked directory with a generic name may be hand-made, and a
  tracked source folder such as `scripts/build/` can hold an ignored `.env`.
  Names match directory components exactly. An ignored parent is archived
  whole, caches inside it included. Files and every other directory are still
  archived.
  Archiving these caches had copied out 1.6 GB of a 3.8 GB tree, so a prune
  moved bytes instead of freeing them.
- Reclaim gives `git worktree remove` `WORKTREE_REMOVE_TIMEOUT` (600s), in
  both `hermes worktree prune` and the startup/cron pruner. A 15-30s timeout
  killed removals mid-delete on a saturated volume and left registered,
  half-deleted trees that every later prune keeps as dirty.
- `hermes worktree prune --dry-run` reports, per tree and in total, how much
  would be copied to the archive. It sizes only those paths, never whole trees;
  the rest of each tree is freed.

## Provenance and patches

- Fork patch identity: `worktree-gc-git-isolation`.
- Fork CI already isolates the real-Git test fixtures under
  `fork-ci-reliability` (see [Fork CI](fork-ci.md)). This unit covers the
  production path those fixtures exercise.
- Upstream: own [PR #124019](https://github.com/NousResearch/hermes-agent/pull/124019)
  carries the same change, head `ed525c0b53f59816cf20ef73e3fdf6e7f97598fe` on
  2026-09-28. Its first production revision listed individual files without NUL
  parsing; review reproduced silent loss of a quoted filename followed by a
  full reclaim, and a later review found ignored-directory symlinks archived as
  regular files. Neither revision was adopted here.
- Surfaces: `hermes_cli/worktree_gc.py`, `hermes_cli/worktree_ops.py` (remove timeout), `tests/hermes_cli/test_worktree_gc.py`.

## Verification

`scripts/run_tests.sh tests/hermes_cli/test_worktree_gc.py`. The regressions
use real Git worktrees: a hostile global `excludesFile` must yield an archiving
verdict, a mixed ordinary and quoted-name untracked directory must archive
every file, a symlink inside an ignored directory must be archived as the same link, a
listed path that cannot be archived must keep the worktree, regenerable directories must
go with the tree while an ignored `.env` (including one inside a tracked `scripts/build/`),
an untracked `dist/` file and an ignored parent holding a cache are archived, a tree with only
regenerable dirt must reap with no archive, and a dry run must report archive size without
sizing a whole tree.

## Retirement and rollback

Retire when a released upstream tag isolates the production GC Git calls from
host config and archives NUL-parsed paths with fail-safe reclaim, with
equivalent real-Git coverage. Roll back by reverting the logical patch and this
unit together. No persistent state changes.
