# Slack status on the legacy API

Load this unit when changing Slack thread status or title calls (`_session_status_method`, `_session_title_method`, `send_typing`, `stop_typing`), or when syncing an upstream change to Slack's Agent Sessions support.

## Required behavior

Slack thread status (`is thinking...`, live tool phrases, the elapsed-time heartbeat, `typing_status_text`) and its empty-string clear go through `assistant.threads.setStatus`, even when the installed slack-sdk exposes `agents_sessions_setStatus`. Thread titles still use `agents.sessions.rename` when available.

`agents.sessions.setStatus` accepts only `active`, `processing`, `suspended` or `closed`. It rejects every free-text status and the empty clear, and it does not clear when the app replies. Hermes debug-logs status failures, so routing free text there hides the indicator with no visible error.

## Provenance and patches

- Fork patch identity: `slack-status-legacy`.
- Cherry-picked from upstream [NousResearch/hermes-agent#110391](https://github.com/NousResearch/hermes-agent/pull/110391) by Baris Sencan, authorship preserved. Upstream issue: [#110374](https://github.com/NousResearch/hermes-agent/issues/110374).
- Introduced by upstream `a5522f69c036`, which routed status through Agent Sessions whenever slack-sdk 3.44+ is installed. The fork's slack-sdk 3.44.1 bump activated it, and LPG's Io showed no Slack indicator from then on.
- Alternative upstream fix [#123457](https://github.com/NousResearch/hermes-agent/pull/123457) keeps Agent Sessions and maps to `processing`/`active`. That loses custom status text and needs an explicit `active` on every exit path, so it is a product decision rather than a defect repair.

## Verification

`scripts/run_tests.sh tests/gateway/test_slack.py` (`TestAgentSessionsApiRouting`). Live check: mention the bot in a Slack channel thread and confirm the status appears during the turn and clears after the reply.

## Retirement and rollback

Retire when upstream merges #110391, #123457 or an equivalent fix that stops sending free text to `agents.sessions.setStatus`. Before Slack's February 2027 removal of `assistant.threads.setStatus`, migrate to the lifecycle enum deliberately. To roll back, revert the cherry-picked commit and this unit.
