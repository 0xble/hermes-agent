# Git tmp-pack holder scope

Load this unit when changing stale Git lock or aborted-transfer pack cleanup in
`hermes_cli/gitlock.py`, its Linux process-ownership helper, or the matching
recovery tests.

## Preserve

- Keep stale lock cleanup conservative: the existing system-wide Git-process
  guard remains authoritative for lock files because a lock holder can close its
  descriptor while still waiting in an editor.
- On Linux, an aged `tmp_pack_*`, `tmp_idx_*`, `tmp_rev_*`, `tmp_mtimes_*`, or
  `.tmp-*` candidate is checked against the target/shared repository metadata and
  every visible process's file descriptors, directory handles, and mappings.
- A Git process in another checkout must not veto cleanup of an unheld pack in
  this checkout. Target/shared repository owners, explicit or unresolvable Git
  resource redirections, unreadable live-holder evidence, and unavailable
  process-table evidence continue to veto. The age floor and inode/mtime
  recheck remain in force.
- Non-Linux behavior retains the existing global process probe.

## Provenance and patches

- **Identity and status:** `gitlock-tmp-pack-holder-scope`, active fork repair.
- **Source / fork refs:** extracted from fork PR #479 commits
  `827626c673874643f322633261ea0a5213d6807d` and
  `80a9e286eac80ee106b890e1ee467b1fa4071fb5`; unrelated shallow-install and
  PM/store changes are intentionally excluded.
- **Surfaces:** `hermes_cli/gitlock.py`,
  `hermes_cli/_git_process_scope.py`,
  `tests/hermes_cli/test_gitlock_process_scope.py`, and
  `tests/hermes_cli/test_gitlock_pack_holders.py`.
- **Contribution route and rationale:** narrow draft PR to restore the Linux
  nightly truthful-outcome behavior after foreign-checkout Git processes caused
  dead transfer packs to survive a successful update.
- **Related evidence:** nightly runs `38548768361` and `38399882810` failed
  with surviving `tmp_pack_*` debris; pre-sync baseline run `38137185573`
  passed. The system-wide `pgrep -x git` guard in `gitlock.py` is the affected
  boundary.
- **Fork delivery:** draft PR against `main`; no merge, activation, or upstream
  public action is part of this unit.

## Verify and recover

Run the focused GitLock suite through `scripts/run_tests.sh`, including the
holder and process-scope tests, and run the truthful-outcome Linux E2E when a
Linux environment is available. On macOS, report that the Linux-only E2E was not
executed rather than treating a local skip as Linux proof. Roll back by reverting
this unit and the focused holder-scope commit together.

Retire when released upstream or the maintained fork provides equivalent
repository-scoped tmp-pack liveness with the same conservative lock behavior and
regression coverage.
