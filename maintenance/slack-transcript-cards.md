# Slack Transcript Cards

Fork patch identity: `slack-transcript-cards`.

## Preserve

`slack-transcript-cards` is a local presentation patch, not a new transcript or routing owner. The existing STT echo ledger and final-priority scope remain authoritative. Named source transcript labels are evidence of speakers, while `SessionSource.user_name` identifies only the uploader. Keep those roles distinct.

## Provenance and Adoption

Upstream comparison: `NousResearch/hermes-agent` main at `4ea8d23cc9f527332ccb1d55359b27741b6ea51b`. Its shared echo still sends localized plain text. Upstream [PR #132435](https://github.com/NousResearch/hermes-agent/pull/132435) proposes a raw-transcript adapter hook, but does not implement Slack cards. No external contribution is authorized for this patch. If that hook lands, use it instead of the private `_stt_transcript` metadata while preserving this presentation contract.

The native Slack sender is the card owner. Relay egress has no negotiated native-transcript rendering contract, so relay retains its legacy textual echo rather than assuming a connector accepts Slack blocks.

The shared echo implementation moves from `gateway/run_inbound.py` into the existing `GatewayVoiceMixin`, and Slack text posting moves into `plugins/platforms/slack/message_delivery.py`, avoiding growth in oversized source files. The existing send pipeline still owns threading, retry/fallback, stream handling, and workspace client selection.

## Verification and Retirement

The behavior proof is `tests/gateway/test_slack_transcript_echo.py`, with existing Slack transport, Telegram voice, and Telegram daily-quota tests as surrounding coverage. Retire this fork unit when released upstream provides the same literal-body, attributed-context contract without regressions in those surfaces. Rollback requires only reverting this patch, no state migration.
