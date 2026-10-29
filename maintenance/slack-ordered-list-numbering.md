# Slack ordered list numbering

Load when changing Slack rich-text list parsing, list grouping, or outbound Block Kit rendering.

## Required behavior

Each ordered `rich_text_list` run starts at its first authored number, even when paragraphs, bullet lists, or a different nesting level split the parent list. Slack's [list element contract](https://docs.slack.dev/reference/block-kit/block-elements/rich-text-list-element) expresses this as `offset = first_number - 1`; omit the offset when the start is 1. Subsequent items retain Markdown's automatic numbering, including lists authored entirely with `1.` markers.

## Provenance and adoption

- Fork patch identity: `slack-ordered-list-numbering`.
- Owner: `plugins/platforms/slack/block_kit.py`; the parser must carry the authored number through indented continuation lines to the list builder.
- Upstream guidance and implementation inspected at `4d3555e5ca1ae3a8babfc33831c3f91eae3a825c`. The renderer still discards the authored number and emits no offset. Closed issue [#57076](https://github.com/NousResearch/hermes-agent/issues/57076) covers only blank-line grouping; preserving that earlier behavior does not fix runs separated by prose, bullets, or nesting.
- The send, finalized edit, and post-native-stream update paths share `_maybe_blocks`; the sanitizer must preserve ordered offsets. Plain-text and native-Markdown paths do not build ordered rich-text list elements.

## Verification and retirement

The behavior contract is `tests/plugins/platforms/slack/test_ordered_list_numbering.py`. Existing renderer and Slack adapter tests remain the surrounding regression surface.

Retire this patch only when the selected upstream release passes that contract without the local implementation. Do not substitute the earlier blank-line-only regression for full coverage. Revert the owned parser/builder change to roll back; runtime adoption remains a separate release decision.
