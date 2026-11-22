# Slack Top-Level File Shares

## Fork Patch Identity

Fork patch identity: `slack-top-level-file-share`.

## Contract

Slack may deliver a top-level DM's document through `file_shared` and `files.info` without putting
the file in the preceding `message.files` event. The fallback must dispatch every file type, retain a
`[file: name (mime)]` marker, and surface a visible attachment notice when metadata or download
fails. A normal message that already included the file must not be dispatched twice.

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
`plugins/platforms/slack/adapter.py`. The contract tests live in
`tests/gateway/test_slack.py` and cover rolling and per-thread DM modes, including a text event
followed by a separate PDF `file_shared` event.

Contract tests: `tests/gateway/test_slack_top_level_file_share.py`.

Retire this patch when the selected upstream release preserves top-level DM documents delivered by
`file_shared` without the local fallback and passes these contract tests. Roll back the commit
carrying `Fork-Patch: slack-top-level-file-share`.
