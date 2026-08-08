# Maintained Hermes mirror

This repository tracks `NousResearch/hermes-agent` while carrying a small set of Brian-owned production patches. The public upstream remains the source of all unmodified Hermes code. `main` is the last candidate that passed the fork verification workflow.

## Maintained patches

The synchronization workflow requires exactly one commit with each subject:

- `chore(local): carry Brian-owned working-tree patches into the fork`
- `fix(state): serialize public reads, bound readers, one gateway SessionDB`

The first commit preserves previously reviewed runtime fixes that have not all landed upstream. The second preserves the SQLite concurrency and reader-budget invariants. Brian-specific skill sources are not maintained here.

## Automatic synchronization

`Fork sync` runs daily and can also be dispatched manually. It:

1. Fetches `NousResearch/hermes-agent:main`.
2. Rebases the maintained commits onto that exact upstream SHA.
3. Refuses a conflict, missing patch, or duplicated patch.
4. Publishes the result to `automation/candidate/<current-main-sha>` without changing `main`.

`Fork candidate` runs the complete canonical Python suite and lint workflow on the candidate. `Fork promotion` advances `main` only when the exact candidate SHA passes and the current `main` SHA still matches the lease encoded in the candidate branch. The candidate branch is deleted after promotion. GitHub suppresses recursive workflow triggers for pushes made with `GITHUB_TOKEN`, so `Fork sync` explicitly dispatches candidate verification after publishing the branch.

Failures open or update a private GitHub issue rather than guessing a conflict resolution or deploying a failed candidate.

## Invariants

- Automation never pushes to `NousResearch/hermes-agent`.
- `main` moves only by an explicit SHA and an explicit force-with-lease.
- Candidate verification never mutates the Personal, LPG, or Meridian runtime.
- Runtime promotion remains a separate operation with its own backup, canary, and rollback proof.
- Patch retirement requires semantic review against the patch-specific regressions. An empty or conflicting Git patch is not sufficient evidence that upstream preserved the behavior.

## Manual recovery

When synchronization is blocked:

1. Inspect the `Fork maintenance blocked` issue and the failed Actions run.
2. Reproduce from the canonical checkout at `/Users/brianle/Repos/hermes-agent`.
3. Fetch `upstream/main` and rebase `main` locally.
4. Resolve only when current upstream behavior and patch intent are both understood.
5. Run the canonical focused and full gates.
6. Push the repaired `main` to `origin` using the remote SHA observed before reconciliation as the force-with-lease value.
7. Close the issue only after remote readback and CI pass.
