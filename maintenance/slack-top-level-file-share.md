# Slack Top-Level File Shares

## Fork Patch Identity

Fork patch identity: `slack-top-level-file-share`.

## Contract

Slack may deliver a top-level DM's document through `message.files`, including a
`file_access: check_file_info` stub, or through the `file_shared` fallback. The message path resolves
stubs before dispatch, caches documents larger than the outbound bot-upload limit, retains a
`[file: name (mime)]` marker, and surfaces a visible attachment notice when metadata or download
fails. A normal message and its `file_shared` event must not dispatch twice.

`dm_top_level_threads_as_sessions` remains a session-keying choice. This patch does not change its
value or interpretation.

## Upstream Design Preflight

The current upstream Slack implementation is the same adapter path as this fork. Merged upstream
[PR #45512](https://github.com/NousResearch/hermes-agent/pull/45512) added the `file_shared` fallback
for videos only, based on the assumption that documents always arrive in `message.files`. Io's
top-level DM evidence falsifies that assumption. Upstream [issue #123608](https://github.com/NousResearch/hermes-agent/issues/123608)
also tracks silent attachment loss at the size-cap boundary. No exact upstream fix for top-level PDF
`file_shared` delivery was found. This patch extends the existing fallback rather than adding a new
transport or session path.

## Ownership And Verification

The owner is `SlackAdapter._handle_slack_file_shared` and the inbound media collector in
`plugins/platforms/slack/adapter.py`. Contract tests live only in
`tests/gateway/test_slack_top_level_file_share.py`, covering rolling and per-thread DM modes,
message-file stubs, visible failures, both event orders, real timestamps, and bot-upload exclusion.

Contract tests: `tests/gateway/test_slack_top_level_file_share.py`.

Retire this patch when the selected upstream release preserves top-level DM documents delivered by
`file_shared` without the local fallback and passes these contract tests. Roll back the commit
carrying `Fork-Patch: slack-top-level-file-share`.
