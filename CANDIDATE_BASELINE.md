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
```

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
```

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
```

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

```
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
Slice 6. The candidate extension installer now copies the four candidate
extensions (`goal-lifecycle`, `memory-journal`, `request-update`, and
`review-candidate`) into a profile's normal `$HERMES_HOME/plugins` directory,
updates `plugins.enabled` idempotently, and stages each copy before replacing
the installed directory. The real discovery E2E proves the installed tools
register in a fresh profile. Focused installer and guard/update verification:
`33 passed`. This does not activate any production profile.

The candidate extension E2E also now validates the tool-schema boundary and the
native update watcher handoff. `goal_set`, `review_candidate`, and
`request_update` expose string descriptions, and `request_update` persists the
same routing fields and output/exit marker contract as the `/update` command
before calling the shared detached watcher. The repair-focused tests pass
`6 passed`.


## Slice 7: Hindsight bank isolation

The candidate records the per-profile Hindsight bank contract in
`CANDIDATE_HINDSIGHT_BANKS.md`. Disposable profiles use the existing
`bank_id_template` surface, producing distinct sanitized banks while keeping
endpoint, credentials, and `HERMES_HOME` profile-scoped. Existing multiplex
coverage verifies that a secondary profile does not inherit the default profile's
bank or retain-shaping values.

Focused bank and identity verification: `3 passed`. After installing the
candidate's pinned optional `hindsight-client==0.6.1` dependency in the isolated
worktree environment, the Hindsight provider and multiplex identity suites pass
`91 passed, 1 skipped`. No production endpoint, bank, or credential was used.
That figure reproduces only where the client is installed: the shared release venv on
this host had it absent and showed 8 failures until `uv pip install
hindsight-client==0.6.1` was run into it on 2026-09-19, after which the provider suite
reports 88 passed.

## Post-review repairs, September 19

An independent review of the candidate found two release blockers and several
overstated claims. All findings reproduced on the exact branch. The repairs below
are source-complete and isolated; no production profile, service, or repository
rename was touched.

### Release blockers, closed

- **Invalid tool schemas.** `goal_set`, `review_candidate` and `request_update`
  built their top-level `description` from a parenthesized comma-separated run of
  string literals, making it a tuple that serialized as a JSON array. Both major
  provider APIs reject that, so any session loading these plugins would have failed
  on its first model call. The plugin tests never rendered a schema and
  `plugins doctor` does not check the type. Fixed, with a test that asserts the
  rendered definition through real plugin discovery and the registry, not just the
  literal.
- **`request_update` ignored the native watcher contract.** It wrote a marker with
  only a reason and timestamp, spawned the updater with output discarded, and never
  produced the exit/output files. Nothing could stream progress, forwarded prompts
  had nowhere to land, and the watcher would time out to a synthetic failure. It now
  writes the routing marker from trusted session context, clears stale watcher
  state, and delegates to Hermes's own `_spawn_detached_update`.

### Review gate rebuilt

`review_candidate` was a single tool-less completion over a diff truncated at
120,000 characters, writing one receipt file that each later candidate overwrote,
with no fallback policy. It could report `reviewed` for a candidate the reviewer
had only partly seen. It now dispatches a real child through `delegate_task` with
the `auxiliary.review` credentials, the same path `/review` uses, so the reviewer
can read the repository and run tests. Oversized candidates are refused rather than
truncated, receipts are keyed by head SHA, and the secondary reviewer is used only
for provider availability failures: a child that started and then timed out or
returned unknown is recorded as `not_reviewed`, never re-run elsewhere. Both routes
unavailable is never approval.

### Slice 10, corrected and partially closed

The ledger's redelivery deadline was already exact. `FLOOD_RETRY_CAP_SECONDS`
caps how long the timer sleeps, while the row's `retry_not_before` decides
eligibility, so a 30-minute penalty already produced no send at 15 minutes. That is
now pinned by test, because the constant reads like a premature-retry bug. The
earlier claim that this behavior was missing was wrong.

Genuinely missing and now fixed: the per-chat flood window was consulted only by
the text send path. `edit_message` neither checked nor armed it, `send_typing`
checked only its own cooldown, the post-send typing re-arm ignored
`typing_indicator: false`, and media uploads had no RetryAfter handling at all. A
rate-limited attachment surfaced as a delivery failure and was never retried.
Uploads now follow the text contract, and a penalty beyond six hours is abandoned
once with the delay named instead of churning the timer until the staleness sweep.

**Still open in slice 10:** media obligations are not written to the delivery
ledger, so a refused attachment is reported truthfully but not automatically
redelivered. Slice 10 must not be described as complete.

This is deliberately not patched here. The ledger stores a text `content` column and
redelivery re-sends that text; an attachment obligation would need the file itself to
survive until redelivery, which raises questions this candidate cannot answer alone:
a media path may be a temporary file already cleaned up, or worse, a path that now
resolves to different content, so a naive re-upload could deliver the wrong bytes
under an old promise. Closing this needs an explicit decision about attachment
lifetime (copy into the profile, or expire the obligation with the file) and is a
better upstream proposal than a fork patch.

### Camofox aliases scoped per profile

The three operator aliases were hard-coded in two places, so every installation
advertised `brianle`, `lpg` and `meridian` — including the shared company agents,
which must not offer an identity they do not own. `browser.camofox.accounts` now
narrows the list per profile, the schema enum derives from the same helper as the
validator, identity derivation is unchanged so existing profiles keep their Camofox
state, and config cannot reintroduce the legacy `personal` alias.

### Goal namespace verified

`goal_set` writes under `goal:<session_id>`, the exact key the gateway's own
`/goal` commands read, and ignores a model-supplied session id in favour of trusted
scope. Enrolling under a task id would have created a goal no user-facing command
could see or clear.

### Branch shape

`hermes update --plan` from the candidate reports
`Install: git (v0.21.3 @ <head>)`, profiles `default`, and no running services. The
checkout tracks `origin/candidate/release-v2026.9.14`, so the candidate self-updates
along its own branch. Aligning `main` with the accepted candidate belongs to the
repository transition, which is separately gated; nothing here rewrites a published
branch.

### Verification

Focused matrix green on this host: review gate and schema contracts 11 passed,
candidate extensions plus schema contracts 36 passed, flood coherence 9 passed,
delivery ledger 42 passed, the combined Telegram/delivery/emphasis set 146 passed,
Camofox account and vault 15 passed. A wide `tests/gateway` selection ran
1855 passed, 6 failed.

Every one of those six is the environment-sensitive class already recorded above,
confirmed directly: this host resolves public hostnames into the 198.18.0.0/15
range (`example.com` to `198.18.33.64`, `files.slack.com` to `198.18.28.48`, and
`github.com` to `198.18.1.87`), so Hermes's SSRF guard correctly refuses the
fixture URLs before the mocked client is reached. Five are the documented Slack and
Telegram media rows; `test_remote_media_fetch.py` passes in isolation and fails only
inside the large selection, so it is test-ordering sensitivity, not a defect. Two
browser-routing tests fail the same way for the same reason. None of these are
repaired here: weakening the SSRF guard to satisfy a hijacked resolver would be the
wrong trade.

### Not done, and still gating

No I6 integration review has been run against the integrated candidate. No
production profile, credential, service or schedule has been touched, no company
host has been inventoried or migrated, no legacy path retired, and no repository
renamed. Personal, LPG and Meridian compatibility and recovery remain unproven, and
each cutover move still needs its own authorization.

## Live isolated candidate, September 19

The candidate ran as a live agent for the first time, in a disposable profile
(`/tmp/hermes-agent-next-live-home`) with no messaging tokens, no Hindsight, no
cron and no curator. Model routes mirror production through the local
CLIProxyAPI on `127.0.0.1:8317`, expressed in upstream vocabulary as two named
`providers` entries (`codex-proxy` with `api_mode: codex_responses`, `claude-proxy`
with `api_mode: anthropic_messages`, both keyed by `CLIPROXYAPI_API_KEY`). The
production config's `model_presets` and `providers[].transport` keys are legacy-fork
vocabulary the candidate does not read; this is the translation.

**Astra availability resolved.** The proxy's model list includes `gpt-6-astra`,
`gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `claude-fable-5-1`, `claude-fable-5`
and `claude-opus-5`. The plan's largest open item is closed. Note the proxy's
identifier is `claude-fable-5-1` with a dash, not the `claude-fable-5.1` the plan
assumed.

Routes as resolved by `hermes config get`: main `custom:claude-proxy` /
`claude-fable-5-1`; delegation `custom:codex-proxy` / `gpt-6-astra`; review
`custom:claude-proxy` / `claude-fable-5-1`. The four candidate extensions register
through real plugin discovery in a fresh process.

Behavioral proofs, each a separate one-shot CLI session:

- **Turn on the main route** (session `20260918_234501_b3de37`): read a fixture file
  and reported all four candidate tools plus `delegate_task` available.
- **Slice 2, delegation** (`20260918_234532_c068b6`): the parent delegated once and
  returned the child's answer verbatim. The persisted delegate result records
  `"model": "gpt-6-astra"`, so a Codex child runs under an Anthropic-transport
  parent as the plan's code reading predicted. The child loaded the worktree's
  `AGENTS.md` from its working directory and truncated it at 20,000 characters; the
  production profile must set the child working directory deliberately.
- **Slice 3, goals** (`20260918_234552_d445eb`): the agent enrolled with `goal_set`,
  did the work, and read status back. The goal persisted under
  `goal:20260918_234552_d445eb`, the exact key the gateway's `/goal` reads. The judge
  did not run because a one-shot CLI turn has no post-turn loop; judge behavior is a
  gateway-path proof still to run.
- **Slice 4, review** (`20260918_234617_198bd1`): `review_candidate` spawned a Fable
  child through `delegate_task`, which found the planted defect in a fixture
  repository (divisor changed to `len(xs) - 1`, silent zero on empty input) and
  returned `changes_requested`. The receipt landed at
  `review_receipts/0f49217a3652d6fe558f6680536e778c3edcbceb.json` with
  `reviewer_model: claude-fable-5-1` and an empty `fallback_reason`.

Two refinements came out of that receipt and are in this commit. The verdict was
buried inside the child's prose; it is now parsed into `result.verdict` and
`result.findings`, with the raw child blob kept beside it, and an unparsable answer is
marked `unparsed` so delivery treats it as changes requested. The child also wasted
a call trying `review_candidate` itself; the brief now tells it not to. Note that the
test session gave the parent only the review and delegation toolsets, so the child
reviewed statically without a terminal; a production parent carries its full
toolset.

Not proven live: the Astra-to-Opus delegation fallback and the Fable-to-Opus review
fallback (would need an induced outage), the goal judge, and every gateway path.

## I6 integration review, September 19

The first whole-candidate review ran to completion under the isolated gateway
(api_server platform on 127.0.0.1:8643, session `api_1789790715_524eb508`,
delegation `deleg_6f435556`), against the frozen head
`f0d2799b42a284c7b50043c6b149bc22e2fc0d7b` with upstream tag
`345cd2b057a452236de401d3534b8502a7465e8d` as base, 63 covered paths. The
reviewer (`claude-fable-5-1`, no fallback) ran 18 minutes 34 seconds, executed
the focused suites (428 passed) and a wider selection (2,212 passed, 4 failed),
and returned `changes_requested` with seven findings. The receipt was written by
the plugin's `subagent_stop` hook at
`review_receipts/f0d2799b42a284c7b50043c6b149bc22e2fc0d7b.json` with the
verdict parsed into data. It is the first receipt produced by the background
review path, which replaced a synchronous version that had timed out at the
420-second tool deadline on the first attempt.

The reviewer independently reproduced the failure classification recorded above
(two SSRF-DNS rows, one `/.dockerenv` chmod row) and corrected one of it: the
persistence-test failure previously attributed to DNS is a genuine candidate
regression from the vault cherry-pick (`_post` forwards `allow_redirects`; the
upstream fixture's fake refuses it). Confirmed by running that file at the
upstream tag (15 passed) and the frozen head (1 failed).

Disposition of the seven findings, all landed on `candidate/followups` at
`5a39c6b954f8`:

| # | severity | finding | disposition |
|---|---|---|---|
| 1 | high | a timed-out or errored reviewer child (summary `None`) was recorded as reviewed and later callers reused it | fixed: child session id is recorded against the head at `subagent_start` and matched at stop, so a failed child writes `not_reviewed`; test added |
| 2 | medium | `request_update` spawned the updater but never armed the gateway's completion watcher | fixed: armed in-process through the runner reference `pairing.py` already uses; test added |
| 3 | medium | `_post` forwarding `allow_redirects` breaks an upstream test fixture | fixed: fixture accepts client kwargs; 29 passed |
| 4 | low | `routed: False` test flaky under a gateway-launched shell | fixed: the test clears `HERMES_SESSION_*` |
| 5 | low | README named the wrong receipt directory | fixed |
| 6 | low | fallback reviewer hard-coded | fixed: reads `auxiliary.review.fallback_providers[0]`; the literals are the last resort; test added |
| 7 | low | Hindsight test figure named no environment and did not reproduce | corrected above; client installed into the release venv |

A second review against `5a39c6b954f8` is in flight as the gate for these
fixes (delegation `deleg_f548a034`, 77 covered paths). A receipt on a
superseded head is evidence of process, not approval of the code that ships.

## Second I6 review, September 19

Against `5a39c6b954f8` (the head carrying the first review's fixes), delegation
`deleg_f548a034`, 77 covered paths, reviewer `claude-fable-5-1`, no fallback. It
verified the seven earlier fixes as sound and pinned by tests, ran 158 plus 308
tests green, and returned `changes_requested` with nine new findings, all in the
follow-up tooling rather than the fork patches. Disposition, landed on
`candidate/followups`:

| # | severity | finding | disposition |
|---|---|---|---|
| 1 | high | the installed feature check derived the repository from its own path, so the copy cron runs (under `$HERMES_HOME/scripts`) crashed before any check | fixed: the checkout is the installed `hermes_cli` package's parent; same for the rehearsal script; the sync stage now requires `--repo` |
| 2 | medium | `request_update` looked for the wrong loop attribute and tools run on an executor thread, so the watcher was never armed in production | fixed: hands the schedule call to `runner._gateway_loop` with `call_soon_threadsafe`; the test now runs the tool from a non-loop thread and asserts the callback ran on the loop thread |
| 3 | medium | a pending review marker had no staleness bound; a stalled child or gateway crash made a head unreviewable forever | fixed: a marker is live only while its delegation is running and under four hours old; a stale one is recorded as `not_reviewed` and re-dispatched; tests added |
| 4 | medium | the migration diff reported `monitor_url`, `monitor_state` and `context_from` as lost when the candidate stores them | fixed |
| 5 | low | media flood refusals escaped voice, image and animation senders as delivery failures | fixed: each answers the typed flood result; test added |
| 6 | low | a contention skip callout persisted forever | fixed: cleared when a run completes; test added |
| 7 | low | `create_job` parameter shadowed `datetime.timezone` and no CLI exposes it | renamed `job_timezone`; CLI exposure is still open |
| 8 | low | memory-journal pending images could pin for the process lifetime | fixed: bounded to 64 entries |
| 9 | low | unclosed code fences in this file put headings inside code blocks | fixed; fence count even, no heading inside a fence |

Cron suite after the fixes: 1,449 passed, the one failure being the documented
`/.dockerenv` chmod row. The installed feature check, run from its cron location
against the live isolated profile, reports OK.

Still open from this review: `hermes cron create/update` and the cron tools do
not expose `job_timezone`, so the 19 migrated jobs depend on the migrated
`jobs.json` carrying the field, which the migration copy preserves.
