# Background topic recovery

Load with the root contract when changing `/background`/`/bg` delivery or Telegram private-topic recovery.

## Required behavior

A detached background command uses the same recovered DM topic as a normal turn. When the recovered topic differs from the inbound lobby, neither the event's reply anchor nor the source's message ID may accompany the task: Telegram must receive the recovered `direct_messages_topic_id` without a cross-topic `telegram_reply_to_message_id`. An unchanged topic retains ordinary reply behavior. Background transcript isolation and media forwarding remain unchanged.

## Provenance and patches

- **Identity and status:** active `background-topic-recovery-066` (archived HERMES-066 at snapshot commit `a3b75d71dfe8`, recorded as `fix(gateway): normalize background topic sources` and `fix(gateway): harden contextual background delivery`; original commits are not available in the shallow archive).
- **Source / fork refs:** archived `session-context.md` HERMES-066; fork base `origin/main` `c8e3342595ea5021c920ba707e70926d20f1f9f2`.
- **Surfaces:** `gateway/slash_commands.py`, `tests/gateway/test_background_command.py`.
- **Upstream disposition:** current upstream `d0288be5b3330d2442e3907185b8e9d0958297bb` still starts the task from raw `event.source`. Issue [#97498](https://github.com/NousResearch/hermes-agent/issues/97498); third-party [#97537](https://github.com/NousResearch/hermes-agent/pull/97537) normalizes the source but retains the old reply anchor, so it does not satisfy the complete delivery contract. Own contribution [#123797](https://github.com/NousResearch/hermes-agent/pull/123797), head `3188be5583d6d1eb7e412dd3132936a207903c92`.
- **Fork delivery:** pending review on this branch; no runtime activation claimed.

## Update and retirement

Compare upstream's command source normalization and Telegram delivery metadata together. Retire the private patch only after a released upstream tag both recovers the topic before task creation and clears cross-topic reply anchors, with equivalent regression coverage.

## Verify and recover

Run `scripts/run_tests.sh -j 6 tests/gateway/test_background_command.py` and the relevant `tests/gateway/` directory, ruff on the changed Python files, `git diff --check`, and `scripts/check_fork_patches.py --source-only --repo .`. Roll back this patch and its row/unit together; no persisted state migration is involved.
