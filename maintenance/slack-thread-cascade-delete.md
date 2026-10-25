# Slack Thread Cascade Delete

Load when changing Slack message edits, tombstone handling, or owner-DM reply deletion.

## Provenance and Adoption

Fork patch identity: `slack-thread-cascade-delete`.

Slack intentionally retains replies and a placeholder after a thread root is deleted. This fork deliberately treats deleting the root in the configured owner's home IM as deleting that thread. The owner may delete either an owner-authored or bot-authored root. See Slack's [message modification contract](https://docs.slack.dev/messaging/modifying-messages) and [chat.delete credential restrictions](https://docs.slack.dev/reference/methods/chat.delete).

The implementation lives in `plugins/platforms/slack/thread_cleanup.py`, called from the changed-message ingress in `plugins/platforms/slack/adapter.py`. The sibling also owns edit normalization, keeping that ingress facade small and preserving the existing processed-message behavior. Cleanup reuses the existing home-channel owner/workspace provenance and secret-source route for `SLACK_USER_TOKEN`, without widening channel or author permissions. The owner token must authenticate in the same workspace. Reply deletion updates the root again, so root-scoped deduplication must run before any await. Fetching all pages before mutation avoids skipping replies as the result set shrinks.

Upstream contribution is not submitted. Fork publication and runtime activation belong to the release owner. Do not silently widen the scope to channels, other DMs, unknown authors, live parents, or offline catch-up during an upstream sync.

## Verification and Retirement

The invariant tests are `tests/plugins/platforms/slack/test_thread_cascade_delete.py`. Existing Slack message-edit and unfurl tests are the surrounding regression surface. Runtime qualification must independently exercise deleting roots from both authors and verify that the parent placeholder disappears.

Retire the patch only when the selected upstream release passes the owner/workspace/author restrictions and the deletion contract without this local implementation. Revert this patch to roll back. No stored state requires migration.
