# Slack Status Protocols

Load this unit when changing Slack thread status or title calls, `adapter_thread_status.py`, the native Stop handler, or upstream Agent Sessions support. Also preserve [thread status continuity](thread-status-continuity.md).

## Required Behavior

Free-text progress, the elapsed heartbeat, `typing_status_text` and the empty-string clear stay on `assistant.threads.setStatus`. Thread titles use `agents.sessions.rename` when available.

Alongside that phrase path, call `agents.sessions.setStatus(status="processing")` once on the first working transition for an exact workspace, channel and thread root. Real completion, interruption and native Stop call `status="active"` for that root. The native endpoint accepts only `active`, `processing`, `suspended` or `closed`, never phrases or the empty clear. Its Tier 3 rate limit must not receive every 2-second refresh.

Reuse `_active_status_threads`. Successors keep their own lifecycle and share one per-root lock across both protocols. Slack's compatibility bridge maps nonempty legacy status to processing and an empty clear to active, so legacy phrases and clears must serialize with native transitions too. Retired owners must recheck ownership after waiting and cannot clear a successor. Native failures are best effort, logged at INFO with the target, and failed active clears retain the target for retry. SDKs without native sessions retain the legacy path. Do not tie native sessions to tool cards or change `display.platforms.slack.tool_progress`.

## Provenance and Adoption

- Fork patch identity: `slack-status-legacy`.
- The phrase-only repair was adopted from Baris Sencan's [NousResearch/hermes-agent#110391](https://github.com/NousResearch/hermes-agent/pull/110391). Related issue: [#110374](https://github.com/NousResearch/hermes-agent/issues/110374).
- Upstream `a5522f69c036` routed free text to native sessions when slack-sdk 3.44+ was installed. Fork revert `edfef31bb32be5183a5329c709721a2b255e511d` preserves the phrase path. Do not reintroduce that routing.
- Native lifecycle supplementation is a deliberate fork adaptation. Reported live reads on 2026-10-30 found native sessions and Stop in DMs but no `agent_session` on active roots across 10 channels, despite visible legacy phrases. [Slack's compatibility bridge](https://docs.slack.dev/ai/migrating-to-agent-messaging/#compatibility-bridge) describes automatic bridging, but [the native setter](https://docs.slack.dev/reference/methods/agents.sessions.setStatus) explicitly creates thread sessions in channels and DMs.
- Upstream main guidance checked at `2d2808ee84549f9836b0056750265058e5488a8a`. Related open alternatives [#123457](https://github.com/NousResearch/hermes-agent/pull/123457), [#143027](https://github.com/NousResearch/hermes-agent/pull/143027) and [#115398](https://github.com/NousResearch/hermes-agent/pull/115398) map phrases to enums or choose a fallback API. They do not preserve simultaneous legacy phrases with transition-only native lifecycle. Keep this difference when reconciling upstream.

## Verification and Retirement

Run `scripts/run_tests.sh tests/gateway/test_slack.py tests/gateway/test_slack_agent_sessions_api.py tests/gateway/test_slack_native_stop.py tests/gateway/test_slack_thread_status.py tests/plugins/platforms/slack/test_session_status.py tests/gateway/test_typing_stale_cleanup.py tests/gateway/test_typing_surface_ownership.py`.

With live-message authorization, mention the bot in a channel thread and verify its root is processing with native Stop during work and active after completion or Stop. Unit coverage does not establish live Slack rollout or UI behavior.

Retire the fork adaptation only when released upstream preserves both protocols, bounded native transitions and successor fencing, or a deliberately accepted migration replaces the phrase path before Slack's legacy API removal. Reverting native supplementation must retain the phrase-only repair. Runtime promotion remains separate from source publication.
