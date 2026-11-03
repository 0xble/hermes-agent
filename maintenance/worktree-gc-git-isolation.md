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
- A lock with a reason belonging to another tool or person is always live: the
  tree is never unlocked or removed, at any age. Upstream treated a foreign lock
  as a leftover, and the sweep deleted another topic's locked eval candidate
  twice (launch-time sweeps ignore the deletion window).
- Every unlock checks ownership immediately before mutation, including frozen
  sweep/audit execution, post-snapshot removal, clean exit, discard, retention and
  failed-add cleanup (including its directory-removal fallback). Unattended
  reclaim refreshes Hermes-lock liveness. Bare, malformed, foreign and unreadable
  locks fail closed. Git's own exact `initializing` reason is the narrow exception
  described below, never a general foreign-lock override.
- `hermes worktree prune --dry-run` reports, per tree and in total, how much
  would be copied to the archive. It sizes only those paths, never whole trees;
  the rest of each tree is freed.

## Git's in-progress-add lock

Internal repository Git pins `LC_ALL=C` and `LANGUAGE=C`; worktree lifecycle
wrappers do the same for add/lock/re-lock, including dashboard adds. Git translates
the persistent in-progress lock reason, so normalizing the writer keeps recovery
exact without treating translated foreign lock strings as ours. This also makes
Hermes' worktree Git stderr English, including diagnostics returned in
`why_kept` and `ArchiveError` messages; the caller's environment is not mutated.

Git writes the exact reason `initializing` while `git worktree add` checks out
files. SIGKILL/timeout can leave that reason, `index.lock`, a partial directory
and the newly created branch behind. Distinguish it from a deliberate foreign
claim:

- Failed-add cleanup may release this exact reason only on the exact empty path
  the attempt atomically reserved before invoking Git, and only once the holder
  probe proves its Git checkout is gone. On timeout, the add's whole process group
  is killed and reaped before cleanup. The guard runs again before any
  raw-directory fallback, so another tool's replacement reason still blocks cleanup.
- A sweep may reclaim it only after the existing per-name age tier, with the
  lock itself older than the cutoff and no fresh checkout activity. A live Git
  holding `index.lock` **or working in that checkout** blocks reclamation; an
  unavailable/incomplete holder probe fails closed. Recheck those proofs at
  execution rather than trusting a frozen plan.
- Ordinary exit/cleanup has no blanket right to release an in-progress add.
  `initializing` with extra text is foreign, like any other unrecognized reason.

The self-healing regression pauses a real checkout in its smudge filter, checks
that Git has written both locks, then SIGKILLs the process group and waits for the
holder probe to confirm its dying descendants have released the checkout. Locale
regressions assert subprocess envs and exercise cleanup, HEAD-fallback retry and
aged sweep with an actually translated Git locale; only those real-locale cases
skip when translations are unavailable. Cleanup must
remove the directory, admin entry and branch and permit the identical retry.
Sweep coverage keeps live and young wreckage before reclaiming the aged
abandoned add; foreign-takeover coverage remains mandatory. An add killed before
its index is installed snapshots an empty staged index (Git's actual state), not
HEAD; otherwise the post-snapshot index comparison falsely reports a concurrent
edit and refuses every reclamation. The archive still holds all partial files.

## Failed-add ownership and process paths

Each add attempt atomically creates its empty worktree directory before invoking
Git. Only that attempt may clean it after failure, including on the local-HEAD
retry; a concurrent named launch that finds the directory leaves it untouched.
Cleanup releases a Hermes pid lock only for this process and releases an
`initializing` lock only after the Git holder is gone. On timeout the add's whole
process group is killed and reaped before cleanup probes holders. Live/unknown
holders and another launcher's Hermes lock are preserved.

Each attempt records whether its branch already exists. An existing branch
without a tree is refused with a clear "branch already exists" message, just as
`_setup_worktree` refuses an existing named tree. Cleanup deletes a branch only
when it was absent at admission and the reserved checkout's admin HEAD proves
the add acquired it; a branch-name race that fails before admin creation is
preserved too. A unique-commit regression covers both normal and HEAD-fallback
bases. A live slow-smudge regression covers timeout cleanup of the path, admin
entry, and new branch, no stray checkout descendants, and a same-name retry.

Process-holder targets must be absolute paths. Linux `/proc/<pid>/fd` links also
name `socket:[...]`, `pipe:[...]`, and `anon_inode:[...]`; resolving those against
the sweeper's cwd would falsely keep an exit-archived tree even when the exiting
process is excluded.

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
- Surfaces: `hermes_cli/worktree_gc.py`, `hermes_cli/worktree_ops.py` (remove timeout,
  foreign-lock liveness and ownership-aware unlock), `hermes_cli/worktree_sweep.py`,
  `hermes_cli/worktree_exit.py`, `hermes_cli/worktree_archive.py` (missing index),
  `cli.py`, `locales/*.yaml` (lock-retention reason), `tests/hermes_cli/test_worktree_gc.py`,
  `tests/hermes_cli/test_worktree.py` (lock reaping).

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

`scripts/run_tests.sh tests/hermes_cli/test_worktree*.py` also covers foreign locks
through startup/classic sweeps, frozen plans/verdicts/audits, ownership changing
during the archive, exit cleanup/discard, retention and failed-add cleanup. The
tree's contents and exact foreign lock must survive each path; existing tests keep
dead-Hermes-lock reclamation and clean exit working.

## Retirement and rollback

Retire when a released upstream tag isolates the production GC Git calls from
host config and archives NUL-parsed paths with fail-safe reclaim, with
equivalent real-Git coverage. Roll back by reverting the logical patch and this
unit together. No persistent state changes.
