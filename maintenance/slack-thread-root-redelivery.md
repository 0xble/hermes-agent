# Slack Thread Root Redelivery

## Required Behavior

Slack `message_changed` events must not replay thread roots when only reply counts, Agent Sessions, link previews, language detection, rendered blocks, or file metadata change. This applies with an empty processed-message cache after restart or eviction. A retained historical `edited` marker does not make a metadata refresh user input.

Compare text and attached file identities with `previous_message` when present. Without a previous snapshot, require an edit marker attributable to the current envelope timestamp, not a historical marker. Legacy envelopes that carry only the original message timestamp can still identify the edit by `edited.ts`. Keep the existing policy that edits of already-routed messages do not trigger another answer.

Genuine edits of unseen messages, including edited-in mentions and failed-enrichment retries, must still route once. New thread replies and tombstone cleanup retain their existing behavior. Rejected metadata events log identifiers and edit evidence, never message bodies.

## Provenance And Ownership

- Fork patch identity: `slack-thread-root-redelivery`.
- Owner: bundled Slack ingress, `plugins/platforms/slack/message_changes.py`, reached through `SlackAdapter._normalize_changed_message`.
- Upstream guidance pinned at `4ea8d23cc9f527332ccb1d55359b27741b6ea51b`: extend the existing adapter, use behavior-contract tests, and move behavior out of oversized facades.
- Related upstream proposal [#118365](https://github.com/NousResearch/hermes-agent/pull/118365), head `7d6199b7269cc90803655c9d7db84ec190f902d1`, compares prior text but misses metadata changes retaining an old edit marker and envelopes without a prior snapshot.
- Related upstream proposal [#131866](https://github.com/NousResearch/hermes-agent/pull/131866), head `9585c347beea43b2c3d613202cf82e19eed200d7`, requires an edit marker but misses retained markers and real content changes without that optional marker. These proposals inform the guard but are not adopted source commits or merged upstream fixes.
- Slack's [changed-message contract](https://docs.slack.dev/reference/events/message/message_changed/) documents envelope `ts` equal to `message.edited.ts` and warns that automatic language detection can emit changes. The failed-enrichment fixture uses the documented edit timestamp rather than the unrelated unfurl timestamp.
- Contribution route: fork-only, no upstream publication authorized. No configuration, dependencies, persistence, or additional deduplication store.

## Verification

Drive `_handle_slack_message` on a fresh adapter and observe delivered user events. Metadata variants cover reply counts, Agent Sessions, retained edits, unfurls, file metadata, and absent previous snapshots. Text changes, changed file identities, and current edits without previous snapshots dispatch once. Existing suites preserve in-flight unfurl suppression, edit retry after failed enrichment, and edited-in mentions.

Run `scripts/run_tests.sh tests/gateway/test_slack_unfurl_duplicate_turn.py tests/gateway/test_slack.py -q --file-retries 0` and the repository's local readiness gate. Before the source fix, the new cases produce `7 failed, 3 passed, 8 deselected` because metadata events reach the user-event sink.

Live verification requires a restarted gateway, a new reply in an existing Slack DM thread, metadata-drop log entries for its old root, and exactly 1 admitted event per new Slack message with no fresh turn for the root. A plain original `message` replay instead of `message_changed` falsifies this diagnosis and requires investigating that separate ingress path.

## Retirement And Rollback

Retire when a released upstream baseline satisfies the complete content-and-edit-evidence contract, including retained-marker and cold-cache cases. Roll back the adapter wiring, normalization sibling, and regression changes together. No state migration or cleanup is needed.
