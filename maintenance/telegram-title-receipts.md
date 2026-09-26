# Explicit Telegram title receipts

## Required behavior

On a Telegram topic lane, `/title` reports the alias actually persisted by the existing lineage setter. A synchronous Bot API rename targets the unsuffixed requested name and the reply distinguishes a successful visible rename from a failed one. Persistence remains successful after a Bot API refusal, exception, stale binding, or unavailable adapter. Non-topic `/title` behavior and automatic title scheduling remain unchanged. An explicit rename must not be disabled by the automatic-rename kill switch. With `auto_topic_icons` on, the explicit rename also re-picks the topic icon from the user's title under the automatic rules (override, model pick through `pick_topic_icon`, keyword fallback) and sends it in the same `edit_forum_topic` call. Unlike automatic renames it replaces a manually chosen icon, because `/title` is the user declaring the topic's subject changed, and the new icon becomes `auto`-owned; a failed or slow pick (bounded at 15 s) renames the title and leaves the icon unchanged.

## Provenance and ownership

- **Identity and status:** active `telegram-title-receipts` fork patch.
- **Source:** archived HERMES-089 (`session-context.md`, archived/main); current fork `origin/main` at `c8e3342595ea5021c920ba707e70926d20f1f9f2` already reserves lineage aliases but returned the requested text and scheduled a best-effort rename.
- **Upstream:** Issue [#100002](https://github.com/NousResearch/hermes-agent/issues/100002) covers duplicate visible labels. The upstream main checked at `d0288be5b3330d2442e3907185b8e9d0958297bb` lacks lineage allocation; [#76454](https://github.com/NousResearch/hermes-agent/pull/76454) is a third-party open PR covering explicit title rename confirmation, while [#86198](https://github.com/NousResearch/hermes-agent/pull/86198) addresses reverse topic-name sync. Neither implements the fork's alias contract. No upstream PR for this fork-dependent receipt adaptation.
- **Adaptation:** Reuse the fork's existing transactional lineage allocation; only the explicit command's acknowledgement and Bot API path change. Do not port the archived title setter wholesale.

## Verification and retirement

Run `scripts/run_tests.sh -j 6 tests/gateway/test_title_command.py tests/gateway/test_session_title_rename_lane.py tests/gateway/test_telegram_topic_icon_lane.py tests/agent/test_title_generator.py tests/test_hermes_state.py -q`; check Ruff and `scripts/check_fork_patches.py --source-only --repo .`. Retire when a released upstream baseline preserves both unique aliases and truthful explicit rename receipts with equivalent tests. Roll back the explicit command and rename helper together while preserving the existing lineage setter and unique index.
