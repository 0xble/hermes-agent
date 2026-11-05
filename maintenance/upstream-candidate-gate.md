# Qualifying upstream-based heads

Fork patch identity: `fork-ci-reliability` (portable contributor gate extension).

`gate.yml` tests the exact PR head, not GitHub's synthetic merge tree. Its
`qualification` job succeeds only if the planner, prompt-surface check, and
**every job in the selected mode** succeed. Only the other mode may be skipped.
The `review_candidate` admission contract is unchanged.

## Mode and trust boundaries

The immutable fork gate ancestor is
`d77a654d8605200b81aea26ab801dba3ea07cf7a` (the first portable-gate commit).
Every descendant must run the existing static/Node gate and all ten Python/E2E
shards. Deleting `bin/ci` fails planning; it cannot select upstream mode. Missing
history, wrong SHA, an unknown gate lineage, and absent upstream entrypoints also
fail closed. The planner executes policy from the frozen base checkout, never
the candidate or merge revision. For bases predating the policy file, the rollout
uses the explicit `ee5a11da4b5bdd61058bba8c9eb4c19fc3fe9585` selector pin for
fork-lineage heads only; upstream heads without staging policy fail. Candidate
edits to the selector cannot change mode selection.

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
checkout, real index, candidate branch or existing ref. It copies exactly three
files from the frozen policy commit:

- `.github/workflows/gate.yml`
- `.github/workflows/prompt-surface-diff.yml`
- `scripts/ci/candidate_mode.py`

All other upstream workflow files are **deleted in the staging overlay only**
to prevent accidentally activating paid Blacksmith runners. The exact candidate
checkout retains its original workflows and candidate-owned entrypoints.

Before selecting upstream mode, the planner refuses unless the staging/head
merge-base equals `B` (candidate) or `B^1` (baseline), the staging base descends
from that anchor, and every overlay commit is single-parent and changes only
those three paths or deletes other direct `.github/workflows/*.{yml,yaml}`
files. It checks **each commit**, not just the net diff: implementation commits,
reverted off-path changes, overlay merge commits, and added/modified unrelated
workflows all fail closed. The final staging tree must contain exactly the two
free policy workflows. Missing history also fails. Ordinary fork ancestry keeps
the full fork gate and does not require a staging base.

### Coordinator procedure (each candidate)

Run from the reviewed policy checkout, with full candidate/upstream/fork history
present. Use full 40-character SHAs. Authenticate through the approved `gh-env.sh`
and require `GH_USER=0xble`. Set `POLICY`, `C`, and a unique `probe` prefix first.
The shell function below supplies the required per-command Git environment; it
changes no global Git configuration or exported shell state.

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
  --body "Evidence only; never merge. Head=$B upstream=$U policy=$POLICY staging=$S"
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
`qualification` is still required for admission; matching baseline failures do
not turn a failed check into a pass. Close both evidence PRs, delete **both**
base/head refs for each, then read back closed PR states and absent remote refs.
Never add harness files to the candidate, rewrite its SHA, or retarget its real PR.

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
or workflow identity. The prompt-surface comparison used the staging tree, which
already included implementation, not the full upstream-base prompt delta. The new
staging-provenance contract was not checked and would reject that base. Re-run
on a clean staging base to obtain full current-policy evidence; retain the old
suite outcomes only under their actual head/policy, never call them a clean
staging qualification. A baseline run is required regardless.


## Residual maintenance

The upstream entrypoint profile must be reviewed when upstream changes its CI
contract. Unknown/missing entrypoints fail rather than receiving a partial pass.
A free runner has fewer CPUs than upstream's paid 32-core machines; slice count,
worker count, and timeout affect scheduling, not the set of required tests.
Tests that fail on an exact candidate are candidate failures, not permission to
narrow the gate. Hosted evidence must distinguish setup failures, suite failures,
and success at the exact recorded SHA.
