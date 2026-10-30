# Session titles: configurable generation, completion retry, and concurrent endpoints

Load this unit with the root contract when changing session title generation, title
scheduling, or title persistence. Platform rename lanes (Slack, Discord, stock Telegram
topics) consume the same generated title.

## Required behavior

- `auxiliary.title_generation` controls word budget, casing, trusted instructions,
  and literal name-alias restoration. Recent titles are supplied as avoidance context;
  malformed or truncated model output leaves the instant derived title intact.
- The title prompt keeps three shortness rules the archived fork had and the first rebuild
  lost: title the concrete subject rather than the message's intent, prefer a proper name,
  and avoid generic leading or trailing labels (Testing, Analysis, generation, help). Examples
  are case-matched Bad/Good pairs, the word rule biases toward the low end of the configured
  range, and the title examples and reply contract close the prompt. Dropping any of
  these measurably lengthens titles; verify prompt changes by sampling the configured model on a few real opening messages, not by unit tests alone.
- Model-generated titles that match another session's name are rejected before persistence,
  ignoring case and a trailing ` #N` on the stored name only. Numbers in a proposed subject
  remain meaningful.
- Paste-only `@file:` openers may title from an explicit usable preview in both
  the instant-title and model-upgrade entrypoints. The raw opener must still pass
  nonempty, type, machine-prefix, and summary guards. Blank, machine-authored,
  and attachment-only previews do not make a file drop titleable. Patch identity:
  `slice-15-title-config`.
- A title the model cannot support does not lock. The schema includes `abstain`; a true
  answer, or a deterministic junk title (`None`, `Investigation`, `Unspecified...`, a platform
  label plus a raw ID such as `Slack Thread <id>` or `YouTube Video <id>`, or a title that is
  mostly IDs), leaves the placeholder in place. A clear opener still starts its model
  title immediately. If that eager first attempt yields no usable title, turn completion
  retries once with the opener plus a bounded excerpt of the assistant reply, without
  waiting for a second user message. It joins any in-flight attempt on the background
  thread, never on the main turn path. A settled `llm` or manually chosen `user` title
  skips that retry. The model's greeting placeholder stays `derived` and also retries:
  a reply can supply a subject even when the opener was only a greeting. If both attempts
  are greetings, the placeholder stays provisional. Each failed attempt is logged, but
  the turn's failure callback reports only the first failure, including a retry-only
  failure. Gateway callbacks remain debug-only, not user-visible. A provisional title
  gets model attempts through turn 3. At turn 3 a rejected name falls back to a title
  derived from the current user message. Only a substantive, non-colliding final
  candidate gets `llm` authority and replaces the instant `derived` placeholder once.
  A rejected fallback stays provisional. Later user turns may recover from their
  literal subject without more model calls or link fetches. An entirely untitled
  session retains its existing model retry eligibility. A manual `/title` still wins, and a stored `llm` or `user`
  title is never replaced. `auxiliary.title_generation.hold_until_reply: false` restores
  the previous opening-message-only upgrade. Patch identity: `title-hold-until-reply`.
- The model title starts with the turn when the turn's self-hosted endpoint is declared
  multi-slot through `providers.<name>.capabilities.concurrent_requests: true`. The opening
  turn's empty or attachment-only opener is the exception: `auxiliary.title_generation.hold_until_reply`
  (default true) waits for that turn's reply before the first model title. Link and prose
  openers start immediately and receive the bounded completion retry described above.
  Links fetch metadata only in the background attempt. A readable page can yield a title
  before the reply, while an unreadable link can abstain and recover from the reply.
  Upstream #117296 defers the title until the turn settles whenever both share a self-hosted base URL,
  which delays a new thread's name by the whole first turn on a routing proxy. The
  declaration is keyed by endpoint URL, never by route name, and an undeclared or `false`
  endpoint keeps the deferral. Equivalent host case/default-port spelling uses the
  shared route identity, and local-server aliases apply the auxiliary router's `/v1`
  completion while bare custom URLs stay verbatim. Capability lookup uses that
  normalized endpoint. A literal `false` vetoes conflicting `true` declarations;
  missing values are neutral and lookup failures retain deferral. Patch identity:
  `title-concurrent-endpoint`.

## Provenance and patches

- **Identity and status:** active fork adaptations `slice-15-title-config`,
  `title-concurrent-endpoint`, and `title-hold-until-reply`. The audit follow-up commit uses the owned identity
  `slice-7-aux-title-routing` for endpoint normalization and conflicting capacity declarations.
  The Telegram icon, lineage-alias and topic-edit identities that shared this unit were
  retired with the fork Telegram stack; see [Telegram stack retirement](telegram-stack-retirement.md).
- **Source / fork refs:** baseline upstream release `v2026.9.14`, fork base
  `origin/main` `a0f8f3996dae`; source design is NousResearch/hermes-agent PR
  [#66353](https://github.com/NousResearch/hermes-agent/pull/66353).
- **Surfaces:** `agent/title_generator.py`, `hermes_state_titles.py`, and the gateway
  title lanes (`gateway/turn_session_titles.py`).
- **Upstream disposition:** the source PR is an open design reference, not released
  equivalent behavior. `title-concurrent-endpoint` has no upstream equivalent; open
  NousResearch PR #120571 also normalizes route identity, while #120627 only widens
  named-provider deferral. Neither supplies the concurrency capability. Own proposal
  [#124563](https://github.com/NousResearch/hermes-agent/issues/124563) now tracks
  explicit endpoint concurrency for auxiliary title scheduling. Retire it when a released
  tag lets a provider declare concurrent capacity for the title gate.

## Update

On every upstream sync, compare title prompt construction and `/title` collision handling
against `agent/title_generator.py`. Retire each patch only when a released upstream tag
provides equivalent behavior, then remove its section and ledger row in the same reviewed change.

## Verify and recover

Run the focused title, gateway, and state tests through `scripts/run_tests.sh`.
`tests/agent/test_title_preview_eligibility.py` exercises preview eligibility through
both title entrypoints with and without the model upgrade. Completion-retry
behavior stays covered by `tests/agent/test_title_completion_retry.py`.
Run `python scripts/check_fork_patches.py` against the worktree HEAD, `git diff --check`,
and `ruff check` on changed Python files. Roll back a patch by reverting the commits
carrying its `Fork-Patch:` trailer; never edit the operator's live config as part of
source rollback. `title-concurrent-endpoint` rolls back alone by reverting its commit: an unread
`concurrent_requests` capability is ignored, so titles on that endpoint simply defer again.
