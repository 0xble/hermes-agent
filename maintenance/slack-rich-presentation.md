# Slack Rich Presentation

Load when changing Slack outbound rendering, image extraction, standalone delivery, or platform hints.

## Required Behavior

With `platforms.slack.extra.rich_blocks` enabled, replies and scheduled/tool deliveries share the renderer and always retain a text fallback. Standalone HTTPS `![alt](url)` lines become images. A paragraph's last line `![thumb:alt](url)` supplies its thumbnail. `-# text` becomes context, optionally prefixed with `![alt](https://url)`. A fenced `cards` YAML list accepts `title`, optional `subtitle`, `body`, `image` and `url`. It emits at most 10 peer cards, using `hero_image` and ordinary title links, never buttons. Invalid or oversized cards remain code, not partially rendered records. Native tables preserve column alignment.

Native-image syntax must not also be extracted/uploaded as attachments. Other images retain legacy extraction, including HTTP URLs. Disabled rich mode must not advertise rich-only syntax. `invalid_blocks` rejection retries the same destination and thread with text, not blocks.

## Provenance And Adoption

- Fork patch identity: `slack-rich-presentation`.
- Owner: `plugins/platforms/slack/block_kit.py`, `rich_media.py`, `adapter.py` and Slack platform-hint selection.
- Upstream guidance inspected at `4ea8d23cc9f527332ccb1d55359b27741b6ea51b`. Searches for Slack carousel/image-block PRs found no equivalent implementation. Retain upstream list/quote/table behavior.
- Slack [card](https://docs.slack.dev/reference/block-kit/blocks/card-block) and [carousel](https://docs.slack.dev/reference/block-kit/blocks/carousel-block) contracts define `hero_image`, title/subtitle 150, body 200 and maximum 10 cards. Without a verified app interactivity handler, emit ordinary links rather than buttons.

## Verification And Retirement

Run `scripts/run_tests.sh tests/plugins/platforms/slack tests/gateway/test_slack_block_kit.py tests/gateway/test_slack_block_kit_adapter.py tests/tools/test_send_message_slack.py` plus focused platform-hint and delivery tests. Test both live-adapter and standalone routes, disabled mode, image de-duplication and rejected-block fallback.

Retire only when the selected upstream release passes these behavior tests without the local patch. Roll back the owned renderer, delivery and hint changes together. Runtime promotion and restart remain separate decisions.
