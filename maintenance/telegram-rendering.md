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

Rich Message prose treats a pair of literal `$` on one line as inline LaTeX, so
two currency amounts in a sentence render as an unwrapped italic formula with
the spaces stripped and the next `$` consumed. On the rich path only, any line
carrying two or more `$amount` tokens has each amount wrapped in inline code.
Closed math (`$x^2$`, `$$…$$`), existing code spans/fences, tickers (`$AAPL`),
and `US$` prose are left untouched. `&#36;`/`&#x24;` outside code are decoded
to `$` on send, edit, and draft before route selection so an HTML-escaped
amount never reaches the user literally.

Telegram's Rich Message parser accepts a block-start `#` run as a heading
without the whitespace standard Markdown requires, so a reply opening with
`#426 review clean…` (or a list item or blockquote starting with `#89`) renders
as a heading. `plugins/platforms/telegram/rich_markdown.py`
(`escape_literal_hash_prefixes`) escapes only such block-start hashes on the
rich path, including inside list-item and blockquote prefixes. Real headings
(`# Title`), inline `#89`, URL anchors, already-escaped `\#`, inline code,
fenced code (including an unfinished fence in a streaming draft), and display
math stay untouched. The escape runs first in `_rich_message_payload`, before
currency protection and linebreak normalization, and is idempotent.

Telegram only renders HTTP(S) and `tg://` link targets as clickable text. Models
can emit Desktop-only `@session:` links or schemeless `[Title](Title)` links,
which Telegram otherwise exposes as raw Markdown. `_degrade_unsupported_markdown_links`
scrubs unsupported targets on both the legacy MarkdownV2 formatter and the rich
payload builder, covering send, finalized edit, and draft. It preserves
supported links, literal code/fenced-code/table regions, and explicitly
bracketed numeric citation markers such as `[[3](https://example.com)]`.
Ordinary numeric commit or PR links are not converted into citation markers.

## Provenance and adoption

Fork patch identities: `telegram-rich-modes`, `telegram-paragraph-spacing`,
  `telegram-rich-currency`, `telegram-literal-hash`, `telegram-link-targets`,
  `conformance-vector-refresh`.

`conformance-vector-refresh` carries no renderer change. The committed conformance
vectors under `tests/conformance/vectors/` are a regenerated snapshot of what the
adapters actually emit, and they had not been regenerated since `29fc746350` in July
while the identities above changed the Telegram adapter five times. The drift that
resulted is real output, not a defect: nested emphasis now renders as `_italic_`
instead of escaping to a literal `\*italic\*`. Regenerate and commit whenever a
renderer under this unit changes, in the same commit, or this gate goes red for
every unrelated pull request.

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

Currency protection: ported from the archived fork's `_protect_rich_currency`
and `_normalize_dollar_entities` (archived commit `41d3a9dc81`, "fix(output):
compose and protect final responses" series) with its regression cases. No
upstream issue or PR covered it when adopted on 2026-09-19. Own contribution:
[upstream PR 116642](https://github.com/NousResearch/hermes-agent/pull/116642),
head `ad2c2da0eb638d90913721ec200f699a09665478`, based on upstream main
`8b42b6e020c3`, open when recorded on 2026-09-20. The upstream head carries the
same adapter symbols; its tests route through tables because upstream has no
`always` mode.

Literal hash escaping: own contribution [upstream PR 105487](https://github.com/NousResearch/hermes-agent/pull/105487)
for [issue 105483](https://github.com/NousResearch/hermes-agent/issues/105483),
head `9bcf0ff987a680731e48167a064af0995dd099d3`, open when adopted on 2026-09-20.
`rich_markdown.py` is byte-identical to that head; the archived fork shipped the
same helper as HERMES-127 (archived commit `6a2dfadcc70d`). The fork adds one
regression for `always`-mode prose opening with a PR number, which upstream
cannot express without an `always` mode.

Telegram unsupported link targets: adopted from the archived fork's HERMES-065
implementation (commits `8c4f3b73129d` and `326aed0f3d21`, scoped citation brackets)
and its focused regressions. The behavior corresponds to upstream issue #97497 and
open PRs #97514/#97535, neither released as of 2026-09-20. The fork intentionally
keeps session-link producer behavior unchanged because this change owns only the
Telegram delivery boundary.

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
that merge, then drop the fork copy. Retire the currency part when a released
upstream `_rich_message_payload` passes the currency regression cases in
`tests/gateway/test_telegram_rich_messages.py` without the fork functions.
Retire the literal-hash part when PR 105487 merges and the candidate tag
contains it: compare `rich_markdown.py` and the hash regression tests against
that merge, then drop the fork copy.

To disable rich rendering, use the supported config command to set this mode to
`never` and restart the gateway. Before rolling back to a boolean-only release,
set it to `true` for adaptive rich rendering or `false` for MarkdownV2. Rolling
back while leaving `always` would reproduce the silent-disable regression.
