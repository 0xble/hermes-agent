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

CommonMark lets an ordered list interrupt a paragraph only when it starts at 1,
so `**Label**\n7. item` renders as one paragraph whose numbers are literal text
with hard breaks and no list layout. `_rich_separate_ordered_lists` inserts one
blank line before such a list when it directly follows a paragraph line. Lines
continuing a list item or blockquote, indented lines, fenced code (including an
unfinished fence in a draft frame), and lists starting at 1 are unchanged. The
insertion is idempotent and counted in the rich length check.

Telegram only renders HTTP(S) and `tg://` link targets as clickable text. Models
can emit Desktop-only `@session:` links or schemeless `[Title](Title)` links,
which Telegram otherwise exposes as raw Markdown. `_degrade_unsupported_markdown_links`
scrubs unsupported targets on both the legacy MarkdownV2 formatter and the rich
payload builder, covering send, finalized edit, and draft. It preserves
supported links, literal code/fenced-code/table regions, and explicitly
bracketed numeric citation markers such as `[[3](https://example.com)]`.
Ordinary numeric commit or PR links are not converted into citation markers.

Telegram renders adjacent footnote references (`[^2][^3]`) as one superscript
run with no separator, so two citations read as "23". Telegram's parser also
drops the first `[^n]:` definition when the definitions directly follow a list,
even across blank lines, leaving a literal `^n` in the list.
`normalize_footnotes` (`rich_markdown.py`) inserts `<sup>,</sup>` between
adjacent references, which joins the same run as "2,3" with each number still
tappable, and puts an empty `<!-- -->` line before definitions that follow a
list, which ends the list without rendering a block. `markdown-it-py` (already a
dependency) finds both: block tokens locate code and lists, and a custom inline
rule marks only references it reaches as prose, so code, TeX math, link
destinations, autolinks, inline HTML and escapes stay literal. It runs on send, finalized edit,
and draft, and is idempotent. Verified against live `sendRichMessage` output on
2026-09-30.

## Provenance and adoption

Fork patch identities: `telegram-rich-modes`, `telegram-paragraph-spacing`,
  `telegram-rich-currency`, `telegram-literal-hash`, `telegram-link-targets`,
  `telegram-ordered-list-separation`, `telegram-footnote-refs`,
  `telegram-progress-literal`.

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
At adoption, the fork carried the adapter symbols and regression file at exact AST
parity with that head. The 2026-09-26 audit refreshed the open PR to
`46dd4994520f683a9bdab216e41f1fefd097a602`. Recheck the final candidate and its
regressions before retirement. The archived
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

Ordered-list separation: fork-authored on 2026-09-26 from a live message whose
`**Facts and Evidence**` label preceded items 7 to 10. Upstream searches found
no issue for this case before the audit created [#124552](https://github.com/NousResearch/hermes-agent/issues/124552).
Open [upstream PR 76368](https://github.com/NousResearch/hermes-agent/pull/76368)
touches the same function but only stops hard-break markers next to block lines.
The paragraph still absorbs a list not starting at 1, so it does not replace
this patch.

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

## Active patch: Telegram tool-progress literal rendering (2026-10-04)

**Status:** Active fork patch `telegram-progress-literal`, based on `origin/main`
`555ccdf1052d644bc1f44b3957e36fe77a813919`. Not upstreamed.

**Behavior:** Tool-progress lines name MCP and connector tools by their friendly
label (`🔌 Paper · get guide`, from `tools.tool_labels.label_for_tool_name`) and
show every interpolated tool label, argument preview, and verbose args dump
verbatim on Telegram. Terminal commands keep their fenced blocks. A compact
preview that is an http(s) URL stays plain text so auto-linking keeps it
tappable, and Discord/Slack keep their own `format_tool_preview` overrides.

**Source surfaces:**

- `gateway/platforms/base.py`: `BasePlatformAdapter.format_progress_literal`
  (identity), base `format_tool_preview` routing non-URL previews through it,
  and `format_tool_event` (the stream-event formatter) routing the label,
  preview, and verbose keys/args through it. `_BARE_HTTP_URL_RE`.
- `gateway/run_turn_runner.py`: `TurnRunner._progress_build_message`, the live
  `progress_callback` line builder, routes the same fragments through the
  delivery adapter's hook. No Telegram checks at call sites.
- `agent/display.py`: `progress_tool_label` (friendly MCP/connector label and
  emoji, honoring `display.friendly_tool_labels` and skin emoji overrides).
- `plugins/platforms/telegram/adapter.py`: `format_progress_literal` →
  `_progress_code_span` (CommonMark code span whose backtick run is longer than
  any run inside, space-padded when the text starts or ends with a backtick).
  `format_message` converts multi-backtick spans to MarkdownV2 single-backtick
  code with `` ` `` and `\` escaped, and its bracket safety net skips escaped
  backticks inside a span. `_RICH_PROTECTED_REGION_RE` opens a fence only at
  line start with a backtick-free info string, so an inline triple-backtick
  span no longer swallows the lines up to a later terminal fence.

**Parse paths (evidence):** progress uses `TurnRunner._send_progress_text`
(`adapter.send` with progress metadata, no `expect_edits`) and
`_edit_progress_message` (`finalize=True`, because Telegram sets
`REQUIRES_EDIT_FINALIZE`). With `rich_messages: always` both reach the rich
API (`sendRichMessage`, rich `editMessageText`) with the raw Markdown from
`_rich_message_payload`. With `auto`/`never` or a rich fallback they take
MarkdownV2 via `format_message`. Both are covered.

**Outbound budget:** no new send, edit, typing, or reaction call site, and no
cadence change from this patch. (Progress cadence is owned by
[telegram-delivery](telegram-delivery.md): 10s for Telegram via
`TelegramAdapter.PROGRESS_EDIT_INTERVAL`, 3s default elsewhere.) The progress lane's call sites
(`_send_progress_text`, `_edit_progress_message`, overflow rolling)
and the Telegram shared per-chat send+edit slot
(`_TELEGRAM_CHAT_OUTBOUND_BUDGET_SECS = 1.0`; progress edits pass
`finalize=True`, so they hold the slot but are not skipped by it) are
untouched, as is `_progress_restore_typing`. The per-conversation worst case is
therefore whatever it was before this patch. Only the text of lines already
sent changes. Wrapping adds two to about eight characters per fragment, so a
very long turn can reach the 4,032-character bubble split slightly sooner and
spend one more overflow send over its lifetime. That send goes through the same
throttled loop, so calls per minute cannot rise.

**Focused regression:** `scripts/run_tests.sh
tests/gateway/test_telegram_progress_literals.py` (15 of 16 fail on the base,
all pass with the patch). Neighbors: `test_telegram_format.py`,
`test_telegram_rich_messages.py`, `test_discord_format.py`, `test_slack.py`,
`test_stream_events.py`, `test_run_progress_topics.py`, `tests/agent/test_display.py`.

**Upstream replacement condition:** retire when a released upstream version
makes the focused regression pass without the fork's `format_progress_literal`
path: the live `progress_callback` line builder and the stream-event formatter
both keep tool names, previews, and args literal on Telegram rich and MarkdownV2
payloads, including embedded backticks, while Discord/Slack URL previews stay
clickable. Open upstream #68182 alone does not qualify (stream-event formatter
only, no embedded-backtick handling).

**Rollback (this patch only):** revert the patch commit. If it shares a squash
with other work, remove `format_progress_literal` and `_BARE_HTTP_URL_RE` from
`base.py` and restore `format_tool_preview` to `return preview.text`, restore
`tool=event.tool_name`/raw keys/args in `format_tool_event`, restore
`get_tool_emoji` and raw `tool_name`/preview/args in `_progress_build_message`,
delete `progress_tool_label`, and in the Telegram adapter delete
`format_progress_literal`, `_progress_code_span`, `_MULTI_TICK_CODE_SPAN_RE`,
`_LINE_START_FENCE_RE`, `_BACKTICK_RUN_RE`, the step-1a block in
`format_message`, the bracket-split regex change, and the line-start fence
anchor in `_RICH_PROTECTED_REGION_RE`. Delete the focused test file. No state,
schema, or configuration migration.

**Upstreamability:** good candidate. The hook is generic (identity base), the
call sites are platform-neutral, and the Telegram encoding is self-contained.
An upstream PR should supersede #68182 by covering the live callback and the
embedded-backtick case. No upstream PR or issue has been opened.

**Independent hypothesis (frozen before upstream tracker search):** Tool-progress
construction interpolates raw tool names and previews into ordinary Markdown. The
editable progress sender passes those strings to Telegram `send()`/`edit_message()`
without a literal-text boundary; when rich delivery is selected, the raw Markdown
is sent unchanged, so `__name__`, `**glob**`, backslash regex escapes, and `.md`
fragments remain parser input. The correction belongs at the shared progress
formatting boundary, not in individual tools or the rich final-response parser.
The smallest complete shape is one adapter hook for literal progress fragments,
used by both `run_turn_runner.py` and `gateway/platforms/base.py`, plus friendly
MCP/connector labels from `tools.tool_labels`. Telegram should encode the marked
fragments for both rich and MarkdownV2 delivery, preserving Discord/Slack preview
URL behavior and safe embedded backticks. The durable regression boundary is the
actual Telegram rich payload and legacy formatted payload produced from progress
lines, covering the screenshot strings, a literal backtick, and the friendly MCP
label. Alternatives considered weaker: escaping only Telegram's final-response
parser misses progress-specific rich sends; patching each call site duplicates the
contract; changing tool names globally would affect logs and model-facing IDs.
Compatibility is additive (base hook remains identity), rollback is deleting the
hook calls and Telegram implementation, and no outbound call type or cadence
changes are intended.

**Reproduction:** `_progress_build_message("mcp__paper__get_guide", ...)` emits
`⚙️ mcp__paper__get_guide: ...`; Telegram `_rich_message_payload` returns that
same raw Markdown. `references/index.md`, `README.md`, `**/*.md`, and `\\s+`
likewise remain parser input.

**Upstream prior art and reconciliation:** No exact released or merged upstream
fix covers the active `progress_callback` plus Telegram rich and MarkdownV2
payloads. Open [upstream PR #68182](https://github.com/NousResearch/hermes-agent/pull/68182)
wraps tool names/previews in backticks in the unused stream-event formatter; its
maintainer review explicitly found that it misses the live gateway callback and
embedded backticks. Merged [PR #42421](https://github.com/NousResearch/hermes-agent/pull/42421)
only restores MarkdownV2 formatting for legacy Telegram progress edits. Closed
[PR #45844](https://github.com/NousResearch/hermes-agent/pull/45844) attempted
rich streaming edits but is not released and does not solve progress-line
interpolation. These findings confirm the frozen boundary and strengthen the
safe-backtick requirement. This fork implementation intentionally diverges from
#68182 by fixing the active runner and base formatter, using the existing
friendly MCP/connector labels, and testing Telegram's final wire payloads.

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
that merge, then drop the fork copy. Retire ordered-list separation when a
released upstream rich payload makes the `TestRichOrderedListAfterProse`
regressions pass without `_rich_separate_ordered_lists`. Retire the footnote
part when Telegram's own rich parser separates adjacent references and keeps
the first definition after a list: resend the probes from the footnote
regressions through raw `sendRichMessage` without `normalize_footnotes` and
check the returned blocks. Rollback removes that function and its call in
`_rich_message_payload`.

To disable rich rendering, use the supported config command to set this mode to
`never` and restart the gateway. Before rolling back to a boolean-only release,
set it to `true` for adaptive rich rendering or `false` for MarkdownV2. Rolling
back while leaving `always` would reproduce the silent-disable regression.
