# Slack Generated Thread Titles

## Contract

Slack DM and Assistant threads keep their instant first-message placeholder, then receive at most one best-effort generated upgrade per cached thread. Channel threads use the same verb-first session title and show it at most once per cached thread on the root's first line, only where the text is ours to edit: a root our bot posted gains a bold first line unless it already leads with one, and the migrated Telegram opener (`*<title>*\nMoved from Telegram...`, posted by an `api_human_users` user through a user token) has only its bold line swapped, through the author's own `SLACK_USER_TOKEN`. Any other human root is never edited. The existing shared session-title worker performs generation off the turn path, preserving endpoint serialization, link context, held-title retries, profile context, and user-title authority. Slack opts into verb-first, sentence-case task titles. Telegram, Discord, and ordinary sessions retain the unchanged noun prompt and echo guard.

Patch identity: `slack-generated-thread-titles`.

`slack.assistant_thread_titles` remains the opt-out for both Slack renames. No new configuration is required. Global `auxiliary.title_generation.enabled` and `model_upgrade_enabled` continue controlling the shared worker.

## Preserve

- Use the platform adapter owning the turn, including workspace-scoped thread keys.
- Send only the LLM stage to Slack, not the session sidebar's derived stage.
- Reserve the bounded upgrade guard before API I/O, including API failures, so later callbacks cannot repeatedly overwrite a manual title.
- Fence the rename on session identity (reset, `/new`, removal), not run generation: a quick follow-up keeps the session and the titler never fires again for it. Respect config changes before the rename. Fail silently with debug logs.
- The verb-first prompt fixes sentence case and ignores `case_style`, because Slack task titles follow Brian's sentence-case label convention.
- Ask the model for about 28 characters, the width Slack's mobile thread header shows, without enforcing it. The 80-character ceiling stays the only hard limit.
- Drop typographic apostrophes, which `agents.sessions.rename` rejects as `invalid_name`. Fall back to `assistant.threads.setTitle` only on `not_authorized`, which legacy Assistant roots return.
- Do not replace the shared worker with an intake-path model request.
- Channel threads do not call `agents.sessions.rename`: Slack documents it for channel thread sessions, but answers `session_not_found` where no agent session exists, and Hermes does not open one there.
- Bot-root detection compares the root's `user` with our bot user ID, never `bot_id`: user-token posts carry the posting app's `bot_id`.
- Send blocks with every root edit when the root has blocks. `chat.update` keeps the old blocks when none are sent, so a text-only edit is invisible.

## Proof and Retirement

`tests/agent/test_task_title_style.py` covers prompt selection, independent echo guards, output length, and background style propagation. `tests/gateway/test_slack_generated_titles.py` covers placeholder upgrade, channel root titling (bot roots, seeded openers only with the author's token, free human text untouched, once only), single-attempt failure, stale/disabled fencing, bounded workspace guards, and the real Slack SDK against the local HTTP stand-in. Existing noun/title, Telegram, and Discord tests remain unchanged.

Upstream prior art: [NousResearch/hermes-agent#74385](https://github.com/NousResearch/hermes-agent/pull/74385) was closed. Retire this patch only when an accepted upstream implementation provides generated task titles through the current title callback with bounded, single-upgrade behavior and preserves noun defaults elsewhere.
