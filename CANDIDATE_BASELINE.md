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
4 passed in 1.16s, including exact-receipt reuse for an unchanged candidate.

## Slice 5 memory journal extension

The candidate extension at `candidate-extensions/memory-journal/` observes
successful built-in `memory` writes, records before/after snapshots in a
profile-local hash chain, and exposes a parent-only `memory_undo` tool. Failed
or staged writes are excluded. Undo refuses stale profile state and refuses
non-latest entries.

Verification:

```text
hermes plugins doctor .../memory-journal --ci    registration passed
tests: candidate-extensions/memory-journal/test_memory_journal.py
3 passed in 0.29s, including a real built-in `memory` tool write.

## Slice 11 Telegram emphasis fix

Applied upstream PR #106906 (`37f872bad17`) to the release candidate. The
candidate now resolves nested and multiline legacy MarkdownV2 emphasis while
preserving code, links, lists, quotes, literals, and the existing fallback
path.

Verification:

```text
tests/gateway/test_telegram_emphasis.py
tests/gateway/test_telegram_format.py
72 passed in 1.42s
```
```
```

## Slices 12 through 14 backup, contention, and update evidence

The candidate records a durable `last_skipped_at` and `last_skip_reason` on a
per-job contention skip without creating an execution row. `hermes cron list`
and `hermes cron status` render the persisted skip. Quick snapshots verify copied
SQLite members before publication and refuse corrupted database members during
restore. Full backup retention now reports incomplete archives as failures and
does not prune older complete archives after an incomplete run. The parent-only
`request_update` extension runs the native read-only update check and, when an
update exists, starts the native detached `update --gateway` watcher through
`.update_pending.json`.

Commits:

```text
f500063ab41  truthful contention skip fields and CLI/status visibility
291fb7bfbf1  incomplete full-archive exit status
3880aa94f0d  retain complete archives after incomplete runs
0c74190b15e  quick-snapshot SQLite verification and restore refusal
9e178344f1a  parent-only native update request
```

Verification:

```text
cron contention + stale-claim tests                 40 passed
cron and CLI cron tests                              36 passed
backup and execution-ledger tests                   107 passed, 1 skipped
backup/update/SQLite guard tests                     98 passed, 1 skipped
request-update extension tests                      2 passed
wide cron + CLI cron suite                           1363 passed, 6 skipped, 1 environment-sensitive failure
```

The wide-suite failure is `test_ensure_hermes_home_sets_0700`: this host exposes
`/.dockerenv`, so the existing container policy intentionally skips chmod while
that upstream test expects owner-only permissions. The focused changed paths
are green, and the candidate worktree is clean.
```

The owned remote is `git@github.com:0xble/hermes-agent-next.git`. It was already
present as a public fork of `NousResearch/hermes-agent`, so no duplicate fork
was created. The candidate branch was pushed and read back at
`437efe5d0a9baa552e462f87525ed15b30cc0e60`. In a disposable profile,
`hermes update --plan` reported `Install: git (v0.21.3 @ 437efe5d)` and no
running Hermes services.

Additional slice-10 delivery patches are now on the candidate branch:

```text
3123777f4d8  Telegram split-send resume, per-chat ordering, and flood cooldown
c65ac8f9e9e  partial-delivery fallback suppression
c1c37ad0146  failed-delivery backoff and last-attempt recovery
```

Focused Telegram and delivery-ledger verification: `133 passed`.

Broader Telegram and delivery verification: `776 passed, 2 skipped`. Two
media URL tests remain environment-sensitive because this host resolves the
fixture `example.com` address into `198.18.x.x`, which Hermes SSRF protection
correctly blocks; the failures are the fixture's blocked-URL path, not a live
Telegram or delivery failure.

## Slice 8: named Camofox account routing

The upstream design preflight read the current upstream `AGENTS.md` and
`CONTRIBUTING.md`, which require extending the existing browser boundary and
searching prior art. Upstream PR #77904 and issue #77273 propose model-supplied
per-call raw `user_id` on every Camofox tool. The candidate takes the narrower
plan-approved session-entry route: `browser_navigate` accepts only the
operator-facing aliases `brianle`, `lpg`, and `meridian`, binds the alias for
the task, and refuses a later switch. `personal` is deliberately not an alias.

Each alias derives a stable profile-scoped Camofox identity from the existing
state helper. Raw `userId` values stay internal. Named-account cleanup drops
Hermes' local task handle and never deletes the Camofox session, preserving
cookies and sibling tabs. The account field is dynamically advertised only
when Camofox is selected, so other browser backends retain their schema.

Focused verification: `48 passed` across named-account, Camofox persistence,
Camofox backend, state, and extension-router tests. The broader browser suite
passed `770` tests with the existing environment-sensitive failures recorded
separately; one compatibility issue found during that run was fixed by keeping
the pre-alias call shape when no account is supplied.

## Slice 9: vault autofill through Camofox

PR #114414 was cherry-picked with provenance across six commits (`68bda7c5a7d`,
`a8141c26d2d`, `4193fb8f343`, `d3ed9196f45`, `e59e49b7a12`, and
`d8a374630ae`). The candidate required two tag-drift adaptations: the
`no_cache_check_fn` decorator is applied after the registry import, and the
multi-origin metadata helpers and `VaultItemMeta.allowed_origins` field are
restored because this candidate tag predates those upstream base changes.

The resulting path uses Camofox's loopback/HTTPS-only evaluate endpoint for
secret-bearing JavaScript, wraps page exceptions with a generic response,
refuses redirects, requires a `current-password` control for login fills,
revalidates the exact allowed origin inside the page, and redacts resolved
values before any result can reach the model. Connect reads use scoped
credentials, refuse partial or redirected configuration, and mint TOTP codes
locally only from usable fields. Browser identity remains the Slice 8 alias;
vault identity remains a separate handle and origin mapping.

Vault and browser verification: `72 passed, 4 skipped` across the vault,
OnePassword, Camofox, browser-vault, and TUI vault suites. No real vault or
account credentials were used.

## Slice 6: canonical skill write guard

The candidate now includes the `canonical-skill-guard` plugin. It resolves the
configured `skills.external_dirs` roots through the existing skill utility,
blocks `skill_manage` writes aimed at an externally owned skill, and directs the
model to `$HERMES_HOME/observations/<skill>.md`. Local profile skills and
unrelated tools remain unaffected. The guard fails closed if it cannot establish
the external roots. `CANDIDATE_SLICE2_CONFIG.yaml` enables the plugin in the
disposable candidate profile.

Focused verification: `3 passed` in
`tests/plugins/test_canonical_skill_guard.py`. The separate dotfiles curation
workflow and generated-install read-only enforcement remain outstanding parts of
Slice 6.


## Slice 7: Hindsight bank isolation

The candidate records the per-profile Hindsight bank contract in
`CANDIDATE_HINDSIGHT_BANKS.md`. Disposable profiles use the existing
`bank_id_template` surface, producing distinct sanitized banks while keeping
endpoint, credentials, and `HERMES_HOME` profile-scoped. Existing multiplex
coverage verifies that a secondary profile does not inherit the default profile's
bank or retain-shaping values.

Focused bank and identity verification: `3 passed`. The broader Hindsight
provider file has eight environment-sensitive failures when the optional
`hindsight-client` and `hindsight_client_api` packages are absent from the
candidate environment; those failures are recorded as dependency setup gaps,
not treated as a pass.
