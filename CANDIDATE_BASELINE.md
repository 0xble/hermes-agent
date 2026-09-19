# Hermes Agent Next candidate baseline

This file records the reproducible baseline for the deployment candidate
before local capability work. It is evidence, not a claim that the candidate is
ready for runtime promotion.

## Candidate identity

- Source: upstream release tag `v2026.9.14`
- Source SHA: `345cd2b057a452236de401d3534b8502a7465e8d`
- Candidate worktree: `candidate/release-v2026.9.14`
- Python: `3.13.5`
- Test interpreter: `/tmp/hermes-agent-next-step1-venv/bin/python`
- Package pins checked: pytest `9.1.1`, anthropic `0.87.0`, httpx2 `2.7.0`
- Production credentials and messaging tokens: absent from the disposable test profile

The test interpreter is outside the checkout because Hermes update tests mutate
the repository virtual environment. Test commands use `env -u PYTHONPATH` so a
different worktree cannot be imported accidentally.

## Bootstrap checks

The isolated release candidate passed:

```text
Hermes Agent v0.21.3 (2026.9.14)
hermes config check: exit 0
```

The candidate also passed the focused capability checks used to begin Slice 2:

```text
tests/tools/test_delegate_request_overrides.py
tests/tools/test_delegate_control_actions.py       59 passed
tests/hermes_cli/test_goals.py
tests/hermes_cli/test_goal_gates.py
tests/hermes_cli/test_goal_judge_delegations.py    63 passed
tests/agent/test_background_review.py
tests/agent/test_review_engine.py                  41 passed
```

These tests verify existing native delegation, goal, and review plumbing. No
runtime patch is justified by this slice's deterministic baseline.

## Remaining focused failures

The release candidate focused run had six failures:

| Area | Result | Classification |
| --- | ---: | --- |
| `tests/computer_use/test_cua_no_overlay.py` | 9 passed, 1 failed, 5 skipped | Host prerequisite absent. The test exercises macOS `CuaDriver.app` resolution, and this machine has no installed app. |
| Slack media tests in `tests/gateway/test_media_download_retry.py` | 11 passed, 3 failed | Host fixture environment. `files.slack.com` resolves here to `198.18.28.48`, which the candidate's SSRF guard correctly rejects before the mocked HTTP client is reached. |
| Telegram media tests in `tests/gateway/test_telegram_media_read_timeout.py` | 2 failed | Same host fixture condition. `example.com` resolves here to `198.18.33.64` and is rejected by the SSRF guard; the later `MagicMock` await error is the test's error path after that refusal. |

These are recorded as environment-sensitive baseline exceptions. They are not
fixed in the candidate because weakening SSRF protection or silently treating
an absent macOS application as installed would move the implementation away
from the required behavior. A future host with ordinary public DNS, or tests
that inject a safe resolver result, should re-run these rows.

## Baseline decision

`v2026.9.14` remains the deployment candidate. Native Slice 2 behavior is
usable and has focused evidence. The six failures stay visible as explicit
environment limitations. The next implementation work begins with the
configuration and instruction contract for delegation, goals, and the single
review gate, while production profiles and the legacy installation remain
untouched.

## Slice 2 configuration check

The credential-free profile example in `CANDIDATE_SLICE2_CONFIG.yaml` was
loaded under `HERMES_HOME=/tmp/hermes-agent-next-slice2-home` with an empty
process environment. `hermes config check` exited 0, and resolved reads
returned:

```text
model.default                         anthropic/claude-fable-5.1
model.provider                        anthropic
delegation.provider                   openai-codex
delegation.model                      gpt-6-astra
delegation.reasoning_effort           high
delegation.max_spawn_depth            2
delegation.max_concurrent_children    10
auxiliary.review.provider             anthropic
auxiliary.review.model                claude-fable-5.1
```

The installed package used for this check was rebuilt in
`/tmp/hermes-agent-next-release-venv` from this release worktree. Its reported
install directory is the release worktree, so the source and installed-package
identities match. No provider request or production profile was used.

## Slice 3 goal lifecycle extension

The candidate extension at `candidate-extensions/goal-lifecycle/` registers
one parent-only `goal_set` tool. It supports automatic enrollment, status
inspection, and additive subgoals. Pause, resume, clear, edit, and replacement
are rejected with `user_control_only`; delegated children are rejected with
`parent_only`. Persistence is confirmed by reading the goal back from the
candidate `state.db`.

Verification:

```text
hermes plugins doctor .../goal-lifecycle --ci    registration passed
tests: candidate-extensions/goal-lifecycle/test_goal_lifecycle.py
6 passed in 1.93s

## Slice 4 review boundary

The candidate extension at `candidate-extensions/review-candidate/` registers a
parent-only, read-only `review_candidate` tool. It requires explicit base and
head commits, validates both with Git, constrains the diff to the requested
scope, routes through `auxiliary.review`, and records a receipt under the active
profile. Unavailable review routes return `not_reviewed`, never approval.

Verification:

```text
hermes plugins doctor .../review-candidate --ci    registration passed
tests: candidate-extensions/review-candidate/test_review_candidate.py
3 passed in 0.56s
```
```
