# Qualifying upstream-based heads

Fork patch identity: `fork-ci-reliability` (portable contributor gate extension).

`gate.yml` tests the exact PR head, not GitHub's synthetic merge tree. Its
`qualification` job succeeds only if the planner, prompt-surface check, and
**every selected non-test job** succeed. Fork heads still require every selected
job green. Upstream-only test failures may satisfy the strict baseline-equivalence
rule below. Only the other mode may be skipped.
The `review_candidate` admission contract is unchanged.

## Mode and trust boundaries

The immutable fork gate ancestor is
`d77a654d8605200b81aea26ab801dba3ea07cf7a` (the first portable-gate commit).
Every descendant must run the existing static/Node gate and all ten Python/E2E
shards. Deleting `bin/ci` fails planning; it cannot select upstream mode. Missing
history, wrong SHA, an unknown gate lineage, and absent upstream entrypoints also
fail closed. The planner executes policy from the frozen base checkout, never
the candidate or merge revision. For bases predating the policy file, the rollout
uses the explicit `cc68f1760a07e6328a673ce2b69e7455b989bce6` selector pin for
fork-lineage heads only; upstream heads without staging policy fail. Its blob
`b71b33d9107e32c4c1b6349679ab204b4cd5f4b7` is byte-identical to the
pre-rebase `ee5a11da` selector and accepts the three frozen arguments. The reachable
rebased commit is required in clean hosted clones. Candidate edits to the selector
cannot change mode selection.

Each execution job checks out `pull_request.base.sha` separately, moves that
harness outside the candidate tree, then checks out and asserts `head.sha`.
Fork bootstrap comes from that frozen base, not the candidate's missing or
changed installer. Upstream bootstrap is the candidate's own locked setup-pm
composite action. There are no write permissions, persisted checkout credentials,
protected archive credentials, or saved PM caches. No `pull_request_target`
execution of untrusted code is introduced.

The upstream mode runs these candidate-owned, unfiltered entrypoints on free
GitHub-hosted Ubuntu runners:

- `python scripts/check --base <frozen upstream merge base>`: every blocking lint
  guard and the code-health ratchet, not just Ruff.
- `node .github/scripts/run-workspace-checks.mjs --concurrency 2`: all workspace
  JS/TS lint, typecheck, and tests, plus the separate WhatsApp Node test suite.
- `cargo test --lib` in `apps/bootstrap-installer/src-tauri`.
- `scripts/run_tests.sh`: the entire ordinary Python suite, partitioned across
  ten required slices using the candidate's own deterministic file partitioner.
- `scripts/run_tests.sh --include-integration tests/e2e`: the entire E2E tree,
  **including upgrade**, in ten required slices. TUI/tmux and usable bubblewrap
  are prerequisites. E2E does not retry failures. Messaging-gated gateway files
  also run once with the messaging SDKs installed; a skipped file fails.

WAL-capable SQLite is required rather than accepting silent WAL coverage skips.
PM reads the candidate's locks and installs both development and test groups.
The prepared PM test environment is exposed through checkout `.venv`, the path
required by the upgrade migration oracle. Upgrade setup fetches upstream CalVer
tags and records the exact historical baseline, and separately requires working
bubblewrap network namespaces instead of allowing hostile-network coverage skips.
Native macOS/Windows, Docker backends, release publishing, credentials-dependent
live tests, and visual release checks remain separate lanes, as with the fork
source gate. This is not a claim to reproduce all upstream release CI.

## PR merge-ref constraint and baseline-first staging

GitHub obtains `pull_request` workflows from the PR **merge ref** and does not
run this event for conflicted PRs. Use qualification-only staging PRs for
upstream candidates that conflict with fork `main`; never merge them. Do not
use `pull_request_target` (wrong check SHA) or `workflow_dispatch`: [GitHub
requires the dispatch workflow on the default branch](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch),
so dispatch cannot prove this draft before landing it.

Freeze candidate `C` and upstream tip `U`, then compute `B = merge-base U C`
independently of any staging ref. **Run the exact baseline `B` first.** Its
staging anchor is `B^1`, not `B`: an overlay descended from `B` has zero head-only
commits and GitHub cannot open the PR. For a merge-commit baseline `^1` means
its first parent, never an arbitrary parent or the merge-base of its parents.
The tested head stays exactly `B`, and `scripts/check --base B` stays unchanged.
A root commit without a parent fails closed (unsupported baseline route).

After recording baseline results, qualify exact `C` against a separate staging
base anchored directly on `B`. The planner computes the upstream comparison
base from `U` and the exact head, **never** from the staging/head merge-base.
Every suite and partition comes from the exact head and is independent of the
staging base. No fork-main changes or candidate commits are required.

### Policy-only construction and refusal

Use `candidate_mode.py --prepare-staging POLICY_SHA` from the reviewed policy
checkout. It uses a private temporary index and `commit-tree`, touching no
checkout, real index, candidate branch or existing ref. It copies exactly five
files from the frozen policy commit:

- `.github/workflows/gate.yml`
- `.github/workflows/prompt-surface-diff.yml`
- `scripts/ci/candidate_mode.py`
- `scripts/ci/baseline_equivalence.py`
- `scripts/ci/gate_test_evidence.py`

All other upstream workflow-tree files (including README and nested helpers)
are **deleted in the staging overlay only**
to prevent accidentally activating paid Blacksmith runners. The exact candidate
checkout retains its original workflows and candidate-owned entrypoints.
Before returning a staging SHA for publication, the helper computes the merge
tree and refuses conflicts or any change to its workflow tree: a candidate may
not re-add upstream workflows or replace policy workflow bytes on the merge ref.

Before selecting upstream mode, the planner refuses unless the staging/head
merge-base equals `B` (candidate) or `B^1` (baseline), the staging base descends
from that anchor, and every overlay commit is single-parent and changes only
those five paths or deletes other files under `.github/workflows/`.
It checks **each commit**, not just the net diff: implementation commits,
reverted off-path changes, overlay merge commits, and added/modified unrelated
workflows all fail closed. The final staging tree must contain exactly the two
free policy workflows. Missing history also fails. Ordinary fork ancestry keeps
the full fork gate and does not require a staging base.

### Coordinator procedure (each candidate)

Run from the reviewed policy checkout, with full candidate/upstream/fork history
present. Use full 40-character SHAs. Authenticate through the approved `gh-env.sh`
and require `GH_USER=0xble`. Set `POLICY`, `C`, and a unique `probe` prefix first.
The shell function below supplies the required per-command Git environment; it
changes no global Git configuration or exported shell state. First write the
probe description to the literal scratch body-file path shown below, beginning
with exact `## Summary` then `## Motivation` headings and recording `C`, `B`,
`U`, `POLICY` and the never-merge intent; retain it as the probe receipt.

```bash
g() { GIT_NO_LAZY_FETCH=1 GIT_OPTIONAL_LOCKS=0 GIT_TERMINAL_PROMPT=0 git "$@"; }
g fetch --no-tags https://github.com/NousResearch/hermes-agent.git main
U=$(g rev-parse FETCH_HEAD)
B=$(g merge-base "$U" "$C")
# Baseline FIRST. stdout is only the new staging SHA; validation goes to stderr.
S=$(python3 scripts/ci/candidate_mode.py . "$B" "$U" --prepare-staging "$POLICY")
g push origin "$S:refs/heads/$probe-baseline-base" "$B:refs/heads/$probe-baseline-head"
gh pr create -R 0xble/hermes-agent --draft --base "$probe-baseline-base" \
  --head "$probe-baseline-head" --title 'Exact upstream baseline qualification' \
  --body-file /Users/brianle/.hermes/cache/scratch/baseline-pr.md
```

Read back the PR head/base SHAs, mergeability and the `Repository gate` run.
Prove the merge-ref gate bytes equal `$POLICY:.github/workflows/gate.yml`
before interpreting the result. Preserve run URL, run `head_sha`, planner
`upstream_base`, all job conclusions and concrete failing test names. Baseline
failures are evidence, not a reason to omit suites or label qualification green.
If upstream moves before a probe starts, verify the logged computed `B` still
matches the frozen expected `B`; otherwise regenerate the paired evidence.

Only after baseline evidence is complete, repeat construction/push/PR creation
with head `$C` and fresh `-candidate-base`/`-candidate-head` refs. Compare actual
failures with baseline before attributing a regression. A successful exact-SHA
`qualification` is still required for admission. Under the rule below, a fresh
complete baseline can admit an explicitly baseline-equivalent candidate. Keep
the baseline probe open with both refs until candidate admission finishes: its
server-side PR association is part of the proof. Close both evidence PRs, delete **both**
base/head refs for each, then read back closed PR states and absent remote refs.
Never add harness files to the candidate, rewrite its SHA, or retarget its real PR.
Never move a published probe base or reopen it after changing policy: GitHub can
retain a stale workflow merge-ref SHA while its API PR association updates. The
verifier refuses that mismatch. Use new unique probe PRs/refs for each frozen
policy revision, and create the candidate probe after the baseline completes.

### Earlier f57e556e evidence

[Run 41956633238](https://github.com/0xble/hermes-agent/actions/runs/41956633238)
tested exact `f57e556e347bb480cbe750b7e6324ee7d1ce4434`. Its staging base
`ac60910951658ad72fcfa56c06215739c51d772e` incorrectly contained three candidate
implementation commits before the three overlay commits; staging/head merge-base
was `0cbcbb159ef11a68be7ba63e3050d86e9469b71c`, not upstream base
`016b76da3a8a88e28775e08b6ca2846f471b8eda`.

The **suite result remains valid exact-candidate failure evidence**, not a pass
and not baseline attribution. Actual planner and lint logs both record
`016b76da3a8a88e28775e08b6ca2846f471b8eda`; all candidate suites/partitions are
base-independent. The staging gate bytes equal the reviewed gate bytes (the
candidate has no gate file), so ancestry contamination did not change execution
or workflow identity. The prompt-surface comparison did use the staging tree,
which already included implementation, but this did **not** change its result:
`tests/fixtures/prompt_surface` has identical tree object
`4eb8005a351f9f5a79254828b21236095adb7733` at upstream base, staging base,
exact candidate and actual merge ref; its diff script is also identical
(`92e9b43247258dae7a69f425b9bf529a231dd2c2`). The unchanged prompt result
therefore stands for the full upstream-base comparison too.

No part of the earlier **execution outcome** must be discarded or re-run solely
for this ancestry mistake: preserve it as exact-candidate failure evidence under
its actual policy. A clean candidate rerun is unnecessary for that conclusion.
The new staging-provenance contract was not checked and would reject that old
base; the old run is not proof of compliance with the new provenance contract,
not baseline evidence, and not successful admission. Run the new exact baseline
first before any baseline-versus-candidate attribution or subsequent candidate
qualification. All future probes must use clean overlay-only staging.



## Upstream-only baseline equivalence

The isolated `qualification` job runs `baseline_equivalence.py` from the frozen
base. It checks out but never executes candidate code. It independently reruns
the ancestry selector with the planner-frozen upstream tip `U`, exact head `C`
and staging base, obtaining `B = merge-base U C`. There are no baseline-run ID,
merge-base or failure-set inputs. Fork-descendant heads never enter this path;
the existing full-green fork aggregator and deletion refusal are unchanged.

All 10 Python and 10 E2E slices, the messaging contract invocation, planner,
prompt-surface, lint, JS and Rust remain mandatory. Each test slice first uses
the candidate's canonical `--generate-slices 10` planner, then runs that exact
file list through `scripts/run_tests.sh --files-from` with retries disabled.
A frozen pytest observer records exact `item.nodeid` values (including parameter
IDs), collection, setup/call/teardown outcomes, exception class and session exit
status. This is machine-readable pytest evidence, not scraped logs or counts.
The observer is added to the disposable PM test venv via a `.pth`, not to candidate
sources. Slice manifests, selected files and per-file sessions are uploaded to
run-scoped artifacts. Source-integrity assertions still run after test failure.

Qualification reads the GitHub jobs and artifacts APIs using an Actions-read,
contents-read token, available only in that isolated job. It requires complete
selected jobs and artifacts; every selected file must have exactly one finished
pytest session. Missing phases, collection/setup/teardown errors, killed workers,
runner/session exit mismatch, empty messaging files, messaging skips, setup,
harness or artifact-upload failures fail. Intentional platform/marker skips stay
supported, but no node executed by the baseline may disappear or newly skip.
All non-test jobs must pass outright; no lint/JS/Rust/build exemption exists.

If all candidate tests pass, qualification reports fully green. Otherwise the
job discovers completed `gate.yml` pull-request runs on **exactly `B`** through
the API, newest first; a baseline must precede the candidate and be an original
complete attempt, never a mixed-job rerun. A failing baseline qualifies as
*evidence*, not as a successful qualification of itself. Old log-only runs are
not usable, including runs 41968636289 and 41979064843: the new machine evidence
and frozen policy are required, not reconstructed summaries.

For each baseline, verification binds the server run ID, exact `head_sha`,
workflow ID/path, event, completed success/failure conclusion and unique live
PR association. The reusable prompt workflow's **server-recorded merge-ref SHA**
must match that PR number; its merge parents must be the API's frozen base and
exact `B`. The baseline overlay ancestry is checked as policy-only, anchored at
`B^1`; all five policy blobs on that base and actual workflow merge tree must
match the candidate's frozen policy byte-for-byte. This rejects a different
workflow with the same check name, candidate-supplied artifact identity, and
policy drift. No live PR association means fail closed; keep baseline refs until
admission and only then perform normal probe cleanup. Deleted/expired artifacts
also fail closed. Comparison checks the selected slice inventory and independently
verified runner/planner Git blobs, not candidate claims about their hashes.
Changed runner/planner code requires full green instead of differential relief.

Candidate call-failure nodes must be a subset of baseline call-failure nodes,
with the **same exception class**. New failures and category changes fail. New
tests may repartition LPT buckets, so comparison is across each full suite while
all 10 buckets remain required. An inherited failure list, both run IDs and both
SHAs appear in the qualification log and job summary under **Baseline-equivalent
(NOT fully green)**. Selected test jobs and the overall workflow remain red when
they really failed; only `qualification` becomes successful differential admission.
`review_candidate` is not changed and still consumes that exact-head check.

### Limits and maintenance risks

- The same node and exception class can fail for different reasons. Exception
  categories cheaply reject obvious changes, but equivalence is not proof of the
  same root cause. Review every inherited node and its trace when deciding to
  land; this gate never represents inherited failures as resolved.
- A flaky baseline failure can mask a new same-node/same-category regression.
  Both sides disable retries; a flaky candidate-only node blocks. Use fresh,
  baseline-first paired runs, not an accumulating failure allowlist. The rule is
  intentionally not statistical flake suppression or a guarantee of test quality.
- `B` is recomputed from frozen `U` in each run. When upstream main moves, an old
  baseline remains usable only if the independently computed exact `B` and frozen
  policy still match. Different `B`, changed policy/runner, incomplete runs or
  unavailable artifacts require fresh proof. No moving branch name substitutes
  for a SHA. Baselines may be reused only while their live association is intact;
  after probe cleanup another candidate needs a new paired baseline.
- Candidate tests still execute candidate code on disposable read-only hosted
  runners, as before. The isolated comparison job never executes it and does not
  trust candidate-provided baseline IDs/sets. This rule does not claim to detect
  malicious tests that lie about their own behavior or dependency/network drift.
  No paid, persistent or self-hosted runners are introduced.

## Residual maintenance

The upstream entrypoint profile must be reviewed when upstream changes its CI
contract. Unknown/missing entrypoints fail rather than receiving a partial pass.
A free runner has fewer CPUs than upstream's paid 32-core machines; slice count,
worker count, and timeout affect scheduling, not the set of required tests.
Tests that fail on an exact candidate are candidate failures, not permission to
narrow the gate. Hosted evidence must distinguish setup failures, suite failures,
and success at the exact recorded SHA.
