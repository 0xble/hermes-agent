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
fail closed. The planner executes policy from the workflow's merge revision;
it does not execute candidate code to choose a mode.

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
Native macOS/Windows, Docker backends, release publishing, credentials-dependent
live tests, and visual release checks remain separate lanes, as with the fork
source gate. This is not a claim to reproduce all upstream release CI.

## PR merge-ref constraint and staging bases

GitHub obtains `pull_request` workflows from the PR **merge ref**, not solely
from the base branch. It does not run this event for a conflicted PR. The refreshed
upstream candidates inspected here conflict extensively with fork `main`.
Changing `gate.yml` cannot create a merge ref for those PRs, and a
`pull_request_target` run would report the base SHA, which the existing review
admission correctly rejects.

For such candidates, use a qualification-only staging base descended from the
candidate's upstream base, with the reviewed fork gate policy overlaid. Keep the
candidate SHA untouched. In that staging base, retain only the gate and its free
prompt-surface workflow: upstream's other workflow files select paid Blacksmith
runners and must not be activated accidentally by a probe. The exact candidate
checkout still contains its own original workflows and entrypoints. The staging
base must contain `scripts/ci/candidate_mode.py` so the clean merge ref carries
this policy. Ordinary fork PRs still target fork `main`.

A staging qualification PR is evidence, **not** a request to merge that upstream
head or the staging base into the fork. Close/delete temporary probe PRs and
branches after preserving their run URLs. Never add fork harness files to the
candidate or rewrite its commits simply to make CI runnable.

## Residual maintenance

The upstream entrypoint profile must be reviewed when upstream changes its CI
contract. Unknown/missing entrypoints fail rather than receiving a partial pass.
A free runner has fewer CPUs than upstream's paid 32-core machines; slice count,
worker count, and timeout affect scheduling, not the set of required tests.
Tests that fail on an exact candidate are candidate failures, not permission to
narrow the gate. Hosted evidence must distinguish setup failures, suite failures,
and success at the exact recorded SHA.
