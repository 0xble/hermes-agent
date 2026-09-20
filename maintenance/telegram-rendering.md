# Telegram rich rendering

## Required behavior

`gateway.platforms.telegram.extra.rich_messages` is one mode: `always` prefers
native rich rendering for every eligible final response, `auto` selects rich-only
constructs, and `never` uses MarkdownV2. Boolean `true` maps to `auto`, `false` to
`never`. The unset default remains `never`. Existing string boolean aliases work;
invalid values raise a configuration error instead of silently disabling rich
rendering. Top-level platform extras retain their existing precedence.

The adapter and the model formatting hint share normalization. Final sends and
finalized edits use the mode. Rich draft previews remain separately configured.
Capability, client-risk, length, flood, and ambiguous-network-result protections
remain owned by the native delivery machinery. This unit does not change any
profile's preview preference.

Rich Message prose paragraph breaks (`\n\n` between two ordinary prose lines)
are materialized as one hard-broken non-breaking-space row because Telegram
clients, iOS in particular, render raw `\n\n` inside a Rich Message with no
visible blank row. Headings, lists, blockquotes, fenced code, tables,
`<details>`, and display math keep their raw boundaries. The normalized payload,
not the source, is counted against the rich character limit. Runs of three or
more newlines collapse to the same single spacer, and normalization is
idempotent.

## Provenance and adoption

Fork patch identities: `telegram-rich-modes` and `telegram-paragraph-spacing`.

Own contribution: [upstream PR 116218](https://github.com/NousResearch/hermes-agent/pull/116218),
head `3d3fed3b68b626a621540993b0bb52853d792765`, based on upstream main
`29bc6343d37`. Open when adopted on 2026-09-19. Related design: [PR 54986](https://github.com/NousResearch/hermes-agent/pull/54986)
by aerbaser proposes a separate `rich_all_markdown` flag. This implementation
instead extends the existing setting with three modes and shares interpretation
with prompt construction. Archived fork commit `5c1aec46a93` implemented the
original three-mode preference. Neither related patch is represented as merged
upstream or directly cherry-picked here.

Paragraph spacing: own contribution [upstream PR 100686](https://github.com/NousResearch/hermes-agent/pull/100686)
for [issue 100664](https://github.com/NousResearch/hermes-agent/issues/100664),
head `c90504124b06c12b24ba9c9d0dca6a3ca479a764`, open when adopted on 2026-09-19.
The fork carries the adapter symbols and regression file at exact AST parity
with that head so retirement is a hash comparison, not a re-review. The archived
fork tracked the same behavior as HERMES-095 (archived PRs #32 and #34).

Fork adaptation retains release `v2026.9.14` and its existing client-risk guards.
It does not import newer upstream approval-header or CJK opt-in changes. On every
upstream sync, read this unit and compare the released adapter, config loader,
prompt hint, and streaming finalization together. A preserved YAML value alone
is not proof that the configured behavior survives an update.

## Verification

Use `scripts/run_tests.sh` with the rich-message, rich-newline, system-prompt,
gateway-config, emphasis, flood-coherence, and final-delivery test files. The
rich-message suite reads real YAML through the gateway and prompt loaders and
observes final send routing for named modes and boolean compatibility. It also
covers ordinary rich finalization, invalid modes, previews, and safety guards.

After authorized promotion, confirm the installed revision, fresh-process mode
resolution, and restarted gateway identity. Inspect an actual final Telegram
response for native rich rendering. Source tests and process health do not prove
client appearance. Read [runtime ownership](runtime-ownership.md) before promotion.

## Retirement and rollback

Retire only when a released upstream version preserves this complete contract,
including existing `always` configuration and prompt/delivery agreement. Keep
this patch while an equivalent upstream proposal is merely open. Retire the
paragraph-spacing part independently when PR 100686 merges and the candidate
tag contains it: compare the adapter symbols and the regression file against
that merge, then drop the fork copy.

To disable rich rendering, use the supported config command to set this mode to
`never` and restart the gateway. Before rolling back to a boolean-only release,
set it to `true` for adaptive rich rendering or `false` for MarkdownV2. Rolling
back while leaving `always` would reproduce the silent-disable regression.
