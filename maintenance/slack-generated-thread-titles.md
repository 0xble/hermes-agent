# Slack Generated Thread Titles

## Contract

Slack DM and Assistant threads keep their instant first-message placeholder, then receive at most one best-effort generated upgrade per cached thread. The existing shared session-title worker performs generation off the turn path, preserving endpoint serialization, link context, held-title retries, profile context, and user-title authority. Slack opts into verb-first, sentence-case task titles. Telegram, Discord, and ordinary sessions retain the unchanged noun prompt and echo guard.

Patch identity: `slack-generated-thread-titles`.

`slack.assistant_thread_titles` remains the opt-out for both Slack renames. No new configuration is required. Global `auxiliary.title_generation.enabled` and `model_upgrade_enabled` continue controlling the shared worker.

## Preserve

- Use the platform adapter owning the turn, including workspace-scoped thread keys.
- Send only the LLM stage to Slack, not the session sidebar's derived stage.
- Reserve the bounded upgrade guard before API I/O, including API failures, so later callbacks cannot repeatedly overwrite a manual title.
- Respect stale-run fencing and config changes before the rename. Fail silently with debug logs.
- Do not replace the shared worker with an intake-path model request.

## Proof and Retirement

`tests/agent/test_task_title_style.py` covers prompt selection, independent echo guards, output length, and background style propagation. `tests/gateway/test_slack_generated_titles.py` covers placeholder upgrade, single-attempt failure, stale/disabled fencing, bounded workspace guards, and the real Slack SDK against the local HTTP stand-in. Existing noun/title, Telegram, and Discord tests remain unchanged.

Upstream prior art: [NousResearch/hermes-agent#74385](https://github.com/NousResearch/hermes-agent/pull/74385) was closed. Retire this patch only when an accepted upstream implementation provides generated task titles through the current title callback with bounded, single-upgrade behavior and preserves noun defaults elsewhere.
