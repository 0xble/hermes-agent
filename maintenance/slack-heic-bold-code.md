# Slack HEIC Attachments and Emphasis Around Inline Code

Load this unit when changing inbound image caching, Slack attachment failure wording, or the Block Kit inline parser.

## Required behavior

- `gateway.platforms.base.cache_image_from_bytes` accepts recognised rasters outside the fast-path magic set (HEIC/HEIF, AVIF, TIFF, ICO). It transcodes them to PNG with `agent.image_routing`'s sniffer and Pillow converter, and stores them as `.png`. SVG, HTML and unrecognised bytes are still refused with "non-image data".
- `plugins/platforms/slack/attachment_errors.describe_download_exception` reports an access problem only when Slack answered with HTML (a sign-in page) or an HTTP 401/403/404. Bytes that downloaded but don't decode are reported as an unsupported format, so the agent doesn't tell the user Slack blocked the file.
- `plugins/platforms/slack/block_kit._inline_elements` holds code spans as private-use placeholders while links and emphasis are parsed, then restores them as `code` elements that keep the surrounding bold, italic or strike. So `**`key`**` renders as bold code rather than literal `**` in rich_text lists, quotes and table cells. Link text and URLs get the original backtick text back.

## Provenance

- Patch identity: `slack-heic-bold-code`.
- Incident: 2026-10-24, Brian's Personal Hermes in Slack (brianle.slack.com). A macOS HEIC screenshot downloaded with HTTP 200 (`image/heic`) was refused by the image cache and reported as "Slack blocked the file download". In the same reply, `**`typing_indicator: false`**` rendered with literal `**`.
- Upstream: NousResearch/hermes-agent#119593 (open) reports the same image-cache refusal for Telegram HEIC documents. No upstream report of the emphasis-around-code defect was found.
- Source surfaces: `gateway/platforms/base.py`, `plugins/platforms/slack/block_kit.py`, `plugins/platforms/slack/attachment_errors.py` (moved from `adapter.py`), `plugins/platforms/slack/adapter.py`.
- Regression: `scripts/run_tests.sh tests/gateway/test_async_media_cache.py tests/gateway/test_slack_block_kit.py tests/plugins/platforms/slack/test_download_failure_diagnosis.py`.

## Retirement

Retire the cache part when released upstream accepts HEIC/AVIF at the image cache (equivalent to #119593's fix), and the parser part when upstream's `_inline_elements` parses emphasis across code spans. Keep `attachment_errors.py` only while its accurate-cause wording has no upstream equivalent.

## Rollback

Revert the `fix(slack): read HEIC screenshots…` and `fix(slack): stop blaming Slack permissions…` commits. No state or configuration changes.
