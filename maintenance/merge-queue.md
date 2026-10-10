# Merge queue

## Required behavior

Every commit on `main` passes the full gate on that exact commit. `main` moves
only by fast-forward to a head that the full gate tested.

- `.mergify.yml` defines two command-only queues. `upstream-sync` (first, so
  `sync/*` and `candidate/*` heads match it before `default`) has
  `batch_size: 1`. `default` batches up to five PRs and waits at most one
  minute. Both use `merge_method: fast-forward`,
  `branch_protection_injection_mode: merge`, queue conditions
  `base = main`, `-draft`, `check-success = pr-checks` and
  `check-success = landing/reviewed-gated`, and the single merge
  condition `check-success = qualification`. `merge_queue.max_parallel_checks`
  is 1. There is no `autoqueue`, no
  `merge_protections_settings.auto_merge_conditions` and no
  `max_checks_retries`: a PR enters only through `@mergifyio queue`, a failed
  batch is never retried to pass, and a dequeued PR never rejoins on its own.
- `.github/workflows/gate.yml` runs on `opened`, `synchronize`, `reopened` and
  `ready_for_review`. Its `profile` job is skipped for ordinary drafts; only a
  draft authored by `mergify[bot]` from a `mergify/merge-queue/` branch runs.
  The profile selects the full gate (the static/Node job plus all ten Python
  shards, each `./bin/ci gate <head-sha>`) for merge-queue batch drafts, and for
  bootstrap PRs while **neither** the PR base nor live `main` has
  `.mergify.yml`. Every other ready PR runs only the bounded `pr-checks` job
  (`./bin/ci preflight` on the exact head), which admits it to the queue.
- `qualification` stays the only required check. It succeeds only when the
  profile chose the full gate and every full-gate job succeeded, so a ready PR,
  a skipped draft or a missing profile fails closed.
- The local lander (`~/.hermes/maintenance/pr-landing/bin/`) keeps its rebase,
  range-diff and exact local gate steps, verifies an approving review receipt,
  posts and reads back `landing/reviewed-gated` on that exact SHA, then marks
  the PR ready and queues it,
  and verifies afterwards that the reviewed head is reachable from `main` and
  that hosted `qualification` succeeded on `main`'s SHA. `0xble/hermes-agent`
  is excluded from the `scripts` repository's auto-merge list.

`tests/ci/test_merge_queue_contract.py` executes the profile and qualification
scripts against fixture repositories and proves that each loosening of the
workflow or queue configuration fails a test.

## SHA-bound admission

A PR-level ready call cannot be pinned to a commit. A push between a head read
and that call could otherwise admit an unreviewed head. The lander therefore
posts a success commit status named `landing/reviewed-gated` only after the
local exact-SHA gate passes and a local review receipt has `status: reviewed`,
the matching `head_sha` and `result.verdict: approve`. An unchanged rebase can
reuse the caller's reviewed-head receipt only after a `git range-diff` of the
complete patch ranges reports only `=`. A rejected exact-head receipt cannot
fall back to an earlier approval. The status is posted on the locally gated
SHA, not on a fresh PR-head snapshot; a later push cannot inherit it.

Both queue rules require this status at admission. Mergify uses
[`check-success`](https://docs.mergify.com/configuration/conditions/#which-check-run-a-condition-reads)
for **both commit statuses and check runs**; `status-success` is not a supported
condition. Only the latest status for a context counts. A status posted by a
person must use the bare context, not a GitHub-App-qualified check name.
[Queue conditions](https://docs.mergify.com/configuration/queue-rules/) govern
admission; do not move this condition to `merge_conditions`, because the
queue's synthetic batch SHA intentionally has not been locally reviewed.
Hosted `qualification` remains the full gate on that batch and on `main`.

Readying a PR by hand, or using another lander that does not verify the receipt,
gate and post this status, no longer admits it. Migrate those callers before
cutover; do not post the status by hand to work around the contract. Existing
admissions and queues need a separate coordinated cutover audit. This context
is an attestation, not a permission boundary: any account with repository
commit-status write permission can forge it. Restrict publisher credentials
operationally; this change does not modify branch protection or permissions.

## Provenance and disposition

Fork patch identity: `merge-queue`.

Fork-only delivery policy. Upstream has no equivalent; nothing to send upstream.
Mergify reads the configuration from `main`, so the PR that adds this unit
lands through the strict non-queue path on its own full-gate `qualification`.
The Mergify GitHub App must be installed on the repository before PRs can be
queued.

- **Rollback:** Revert the commits carrying `Fork-Patch: merge-queue`. Revert
  `.mergify.yml` and `gate.yml` together: the gate without the queue would leave
  ready PRs unable to earn `qualification`.
