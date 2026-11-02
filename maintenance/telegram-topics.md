# Telegram topic titles and icons: title generation, Bot API icons, and lineage labels

Load this unit with the root contract when changing session title generation, Telegram
DM topics, or title persistence. The three fork patches are coupled: a model title,
its optional icon, and the durable alias can be observed through one Telegram topic.

## Required behavior

- `auxiliary.title_generation` controls word budget, casing, trusted instructions,
  and literal name-alias restoration. Recent titles are supplied as avoidance context;
  malformed or truncated model output leaves the instant derived title intact.
- The title prompt keeps three shortness rules the archived fork had and the first rebuild
  lost: title the concrete subject rather than the message's intent, prefer a proper name,
  and avoid generic leading or trailing labels (Testing, Analysis, generation, help). Examples
  are case-matched Bad/Good pairs, the word rule biases toward the low end of the configured
  range, and the icon request sits inside the rules so the title examples and reply contract
  close the prompt. Dropping any of these measurably lengthens titles; verify prompt changes by
  sampling the configured model on a few real opening messages, not by unit tests alone.
- Telegram topic icons use Bot API `get_forum_topic_icon_stickers` options only.
  Name and icon changes share one `edit_forum_topic` call. An icon-only `/topic edit --icon`
  omits `name`, so Telegram keeps the visible name; resending the saved session title
  reverted an earlier `--title` rename. Manual icon ownership is
  preserved when configured, and successful automatic writes record state and history.
- Legacy topic-icon schema rebuilds stay inside the `SessionDB` writer transaction.
  A failure rebuilding either icon table, creating later indexes, or writing the
  schema-version marker must roll back the exact pre-migration schema and rows.
  Retrying preserves manual icon ownership and profile isolation. Patch identity:
  `slice-16-topic-icons`.
- Icon choice favors relevance, with variety only among relevant icons. The model is always
  offered the whole catalog; withholding recently used icons made the obvious icon unavailable and
  forced unrelated picks. The model returns up to three ranked icons (`icons`, with the older
  single `icon` still accepted), and `pick_ranked_icon` takes the best one outside the chat's last
  `ICON_COOLDOWN` (5) automatic icons, falling back to the top pick when all are recent. It never
  selects an icon the model did not rank, so the cooldown cannot reintroduce unrelated picks. The
  structured-output schema must list `icons` when icons are requested, or providers that enforce
  the schema silently drop them. Prompt wording that asked for extras "only if" they clearly fit
  made the live title model return a single icon almost every time, which disables the cooldown.
  Without operator `topic_icon_instructions`, `DEFAULT_ICON_GUIDANCE` asks for the most specific
  recognizable icon. The keyword fallback uses recency only to break ties and returns no icon
  (the topic keeps its current one) when nothing matches. The adapter warms the icon catalog
  after connect so the first topic after a restart also gets a model pick. Each choice logs
  its path (`override`, `model`, `keyword`, or `none`) at INFO. Verify guidance changes by
  sampling the configured title model on real opening messages against the live catalog.
- `/title X` on a Telegram topic stores the user's exact text, or the next free lineage alias
  such as `X #2` only when that exact text is held by another session; the visible Telegram
  topic is renamed to the unsuffixed label. Model-generated titles that match another
  session's name are rejected before persistence, ignoring case and a trailing ` #N` on the stored name only. Numbers in a proposed subject remain meaningful. Background title
  callbacks use their persisted alias so even a collision arriving between the check and
  persistence cannot show a duplicate label.
- `/title X` also re-picks the icon from `X` when `auto_topic_icons` is on, through the same
  selection helper as automatic renames, and replaces a manual icon (automatic renames never do).
  Any icon Hermes writes clears the adapter's remembered manual icon for that topic; see
  [explicit title receipts](telegram-title-receipts.md).
- `preserve_manual_topic_icons` relies on a Bot API `StatusUpdate` handler that observes
  `forum_topic_created`/`forum_topic_edited` service messages in private chats; a user-chosen
  icon recorded there is never replaced by an automatic rename.
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
  which delays a new topic's name and icon by the whole first turn on a routing proxy. The
  declaration is keyed by endpoint URL, never by route name, and an undeclared or `false`
  endpoint keeps the deferral. Equivalent host case/default-port spelling uses the
  shared route identity, and local-server aliases apply the auxiliary router's `/v1`
  completion while bare custom URLs stay verbatim. Capability lookup uses that
  normalized endpoint. A literal `false` vetoes conflicting `true` declarations;
  missing values are neutral and lookup failures retain deferral. Patch identity:
  `title-concurrent-endpoint`.

## Configured Cron Topic Targets

A numeric Telegram destination selected by a cron job is a send target, not an
ownership transfer. The router validates a live, visible, unended session binding
in the adapter's profile without consulting or changing the historical
`cron:<job_id>` lane. This includes jobs whose `origin` still names an old topic
and jobs with `keep_pinned_lane` enabled. That authored flag controls lane
conversion, not the Telegram owner registry.

Explicit `telegram_topic_owner_key` metadata and named-topic creation retain
strict owner validation. They cannot follow an unrelated topic or take over its
owner. Missing, ended, archived, hidden, and cross-profile numeric destinations
remain invalid on the live lane. This correction does not change the scheduler's
standalone fallback policy.

`tests/gateway/test_telegram_topic_owner_routing.py` covers a stale cron lane,
unchanged canonical owners, explicit-owner refusal, and invalid destinations.
`tests/cron/test_cron_live_delivery_confirmation.py` checks that the configured
thread wins over a pinned job's stale origin before the send.

## Provenance and patches

- **Identity and status:** active fork adaptations `slice-15-title-config`,
  `slice-16-topic-icons`, `slice-17-topic-lineage`, `slice-9-telegram-topic-edit`,
  `title-concurrent-endpoint`, `title-hold-until-reply`, and `telegram-topic-owner-routing`. The audit follow-up commit uses the owned identity
  `slice-7-aux-title-routing` for endpoint normalization and conflicting capacity declarations.
- **Source / fork refs:** baseline upstream release `v2026.9.14`, fork base
  `origin/main` `a0f8f3996dae`; source designs are NousResearch/hermes-agent PR
  [#66353](https://github.com/NousResearch/hermes-agent/pull/66353) and the
  single-call title+icon idea in [#35737](https://github.com/NousResearch/hermes-agent/pull/35737).
- **Surfaces:** `agent/title_generator.py`, `agent/topic_icons.py`,
  `hermes_state_titles.py`, `hermes_state_telegram.py`, gateway topic/title lanes,
  and `plugins/platforms/telegram/adapter.py`.
- **Adaptations:** Bot API only; no MTProto, Telethon, custom packs, provider key,
  attachment forwarding, or new environment variables. Icon selection falls back
  deterministically when the model field is absent or invalid.
- **Upstream disposition:** source PRs are open design references, not released
  equivalent behavior. `title-concurrent-endpoint` has no upstream equivalent; open
  NousResearch PR #120571 also normalizes route identity, while #120627 only widens
  named-provider deferral. Neither supplies the concurrency capability. Own proposal
  [#124563](https://github.com/NousResearch/hermes-agent/issues/124563) now tracks
  explicit endpoint concurrency for auxiliary title scheduling. Retire it when a released
  tag lets a provider declare concurrent capacity for the title gate.
- **Fork delivery:** landed on fork `main` as one squash-merged PR whose commit carries the
  `Fork-Patch:` trailer slices above. This unit is the owner of those identities;
  `scripts/check_fork_patches.py` verifies the trailer on every commit after the floor.
  The published squash commit for PR #638 omitted its trailer; the stable patch-id backfill
  below repairs classification without rewriting `main`.

Fork-Patch-Backfill: b91a64164821e8c1a3669a3b5e153f871370e86c; telegram-topic-owner-routing
Fork-Patch-Backfill: 3d381f0050b51c28e63a482435e87a0b8380d59a; telegram-topic-owner-routing

## Update

On every upstream sync, compare title prompt construction, Telegram adapter topic
editing, topic state migrations, and `/title` collision handling together. Preserve
manual ownership and the one-call rename invariant. Retire all three patches only
when a released upstream tag provides equivalent behavior, then remove this unit and
its ledger rows in the same reviewed change.

## Verify and recover

Run the focused title, icon, gateway, and state tests through `scripts/run_tests.sh`.
`tests/hermes_state/test_telegram_icon_migration.py` injects failures at both
legacy icon-table drops, a later index, and the schema-version write, then checks
exact rollback and successful retry.
`tests/agent/test_title_preview_eligibility.py` exercises preview eligibility through
both title entrypoints with and without the model upgrade. Completion-retry
behavior stays covered by `tests/agent/test_title_completion_retry.py`.
Run `python scripts/check_fork_patches.py` against the worktree HEAD, `git diff --check`,
and `ruff check` on changed Python files. If rollback is required, revert the landed
squash commit (found by its `Fork-Patch:` trailer) as one unit and retain the two icon tables
(`telegram_topic_icon_state`, `telegram_topic_icon_history`) before starting a process on the
rolled-back code; never edit the operator's live config as part of source rollback.
`title-concurrent-endpoint` rolls back alone by reverting its commit: an unread
`concurrent_requests` capability is ignored, so titles on that endpoint simply defer again.

## Contract tests

Contract tests: `tests/gateway/test_telegram_topic_owner_routing.py`,
  `tests/hermes_state/test_telegram_icon_migration.py`,
  `tests/agent/test_title_preview_eligibility.py`,
  `tests/agent/test_title_completion_retry.py`,
  `tests/cron/test_cron_live_delivery_confirmation.py::TestConfirmAdapterDelivery::test_configured_target_wins_over_pinned_origin`
