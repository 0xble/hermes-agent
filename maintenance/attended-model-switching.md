# Attended model switching

## Required behavior

When `model.attended` is configured, genuine user activity in a topic may route the next gateway request to the attended model and reasoning effort until the configured idle window expires. New sessions start on the attended model by default when `model.attended.start_new_sessions` is true; their first route is silent. Relays, wakeups, reactions, edits, and internal messages do not reset the user-activity clock. Explicit session and one-turn model choices retain their precedence. Idle expiry is committed only at a real API-request boundary, and only when both the genuine-activity window and the session's API-call quiet window have elapsed. A session opened by automation, such as a topic split or relay, has no genuine-activity stamp yet, so it stays attended until the user's first message there starts the idle window. If the request is already at the default model's compression threshold, Hermes keeps the attended route and does not switch or compact for this purpose.

The legacy `model.compact_before_switch_tokens` key is still accepted for compatibility but is deprecated and ignored. Normal per-model preflight compression is the only compaction path.

When the attended configuration is unset or disabled, all attended activity tracking, route resolution, notices, compaction, and model-switch hooks are no-ops. Existing gateway call shapes and default model behavior remain unchanged.

## Precedence and Telegram topic edits

The gateway resolves a lane in this order: `--once`, explicit session picks,
temporary picks, a held `@role`, active `model.attended`, then the
channel/global fallback. A held role follows the live preset on every request
and does not post a switch-in or switch-back card. A session `/model reset`
removes the persisted model pick and the role, including when attended
switching is unconfigured but a role is set. `/model reset --all` clears both
on every session. A durable `/model X` or `/model X --session` replaces the
role; `/model X --once` wins for that turn and leaves the role in place. A
`/reasoning default` or `/reasoning reset` removes the persisted reasoning
pick. Plain picks remain temporary while attended switching is enabled and
expire after the idle window. Temporary picks preserve a named hold and expire
back to it, just as `--once` preserves it.

Role assignment is bound to a turn, not to its starting config snapshot. Busy
topic edits and bulk conversions take effect next turn, even before the current
turn's first API request. Gateway turn assembly captures assignment synchronously
and supplies it to TurnRunner. Callback-only construction does not read routing
state. At each real request,
the current assignment follows fresh model, provider and reasoning config from
the owning profile through the existing preset loaders. An empty `@attended`
or deleted preset falls back coherently to the configured channel/default
route, without triggering automatic attended routing or deferring to a prior
attended decision. Inherited reasoning is resolved for that destination from
the same fresh profile snapshot. Explicit session effort wins over preset
effort, then destination per-model policy and global effort. Keep the canonical
reasoning dictionaries until effort normalization so disabled values and
provider-specific tiers survive the decision key and subsequent requests.
Channel fallbacks use the gateway's existing typed channel overrides, just as
runtime route setup does.

Role destinations pass the existing cost, data-training and context-cache
selection guards before publication. Slash confirmation retains the role
reference, not the transiently resolved concrete destination.

Role writes share the concrete override lock, epoch fences and cancellation
settlement path. A successful role edit invalidates pending one-turn restores.
Bulk conversion locks eligible routing keys in stable order, rechecks their
eligibility after waiting, then persists one staged snapshot before publishing
live replacements. If authoritative and fallback persistence both fail, neither
memory nor durable routing changes, including on a later unrelated save.

New/reset boundaries await changed role persistence off-loop. Resume and
compression repoints preserve the hold. Unchanged role state is not rewritten,
and compression-exhaustion reset reuses its already-held override lock.

`telegram_topic` model edits are explicit operator picks, not ambient topic
configuration. They use the same session model-override store and default
scope as `/model --session` (`model_override_scope="session"`), so they survive
idle expiry and gateway restart and take precedence over `model.attended`.
This applies to both `create` and `edit`; queued edits change the durable
next-turn override without swapping a running agent.

The reasoning resolver checks the persisted/in-memory session reasoning pick
before the attended reasoning fallback. A topic edit's explicit reasoning pick
therefore also remains pinned; `default` and `reset` clear it and allow the
attended/default resolver to run again.

## Administrative reset

`/model reset --all` is an explicit gateway-admin operation. It clears every
session's persisted and in-memory model override, reasoning override, and
model role through the locked SessionStore/model persistence paths, including
the `gateway_routing` state in `state.db` and the `sessions.json` mirror. It
is available regardless of whether attended switching is enabled and reports
the number of sessions that had an explicit pick or a role. A session with an
active turn is persisted as reset but its current agent route is left alone;
the next turn resolves from the default route. With slash-admin gating
disabled, the fallback authorization is limited to a configured owner's direct
message; group messages are denied.

The attended switch-in card is rendered by one helper in
`gateway/attended_model.py` as a single sentence-style line, like the `/fast`
expiry notice. A fresh session starts attended without a switch-in card. Idle
expiry emits a separate back-to-default line and leads with the `⇄` symbol.
For example, with a `gpt-6.1-sol` default and a 15 minute window:

```text
⇄ Back to GPT-6.1 Sol after 15m idle.
```

## Provenance and patch

Fork patch identities: `attended-model-switching`, `gateway-telegram-topic-tool`, `live-model-preset-roles`.

`live-model-preset-roles` covers `@role` holds: the role field on conversation
state and `SessionEntry`, role reset, concrete-pick precedence, live preset
follow, and the silent held-role card. The earlier identities cover attended
routing and the Telegram topic tool.

Preset syntax, route readers, reasoning precedence and config persistence are owned
by [Model presets](model-presets.md). This unit adds only session role state,
API-boundary config-follow, attended holds, topic commands and bulk conversion.

`/model --all attended` writes the converted sessions in one routing snapshot.
A topic role edit reads reasoning from the same routed profile as the route.

Fork-Patch-Backfill: cd3d18e27b6e7bf54a5f3bb22d81c571e3e2e95d; live-model-preset-roles

The attended feature adds per-topic attended routing, idle expiry, persisted activity timestamps, and route re-evaluation at the real API-request boundary. Keep its disabled path inert so users without the new keys receive the pre-feature behavior exactly. The topic tool commits through the gateway's existing model and reasoning persistence paths rather than adding a second store.

The combined resolution is implemented in `gateway/run_turn.py` and
`gateway/run_config_loaders.py`; the topic commit path is
`gateway/telegram_topic_sessions.py` and `gateway/slash_commands_model.py`.
`@role` holds persist on `SessionEntry.model_role` and resolve through the shared
config route resolver in `hermes_cli/model_preset_runtime.py`. `/model --all attended` converts concrete Telegram
overrides to that role and returns the converted session keys, skipping keys
already on a role.
This is a fork-only gateway feature that adds per-topic attended routing, idle
expiry, persisted activity timestamps, and boundary-only idle expiry. A new genuine user message switches in at the next API call; idle expiry switches back to the default only when the session is quiet and the request fits the default route. A long autonomous turn that continues making API calls stays on the attended route. An active fallback is never undone. The deprecated `compact_before_switch_tokens` key remains accepted but is ignored; normal per-model preflight compression remains the only compaction path. Keep its disabled path inert so users without the new keys receive the pre-feature behavior exactly.

## Verification

Run `tests/gateway/test_model_role_lifecycle.py` for request-boundary profile
edits, busy assignment timing, stale hydration/one-turn restores, failed
snapshot publication, transient selection guards and guarded async boundaries.
The lifecycle suite builds turns through the gateway assembly boundary. Run it
with `test_cached_agent_history_guard.py`, `test_local_model_connection_reply.py`,
`test_multiplex_session_db_profile_scope.py`, `test_native_stream_warning_composition.py`,
`test_slack.py`, `test_streaming_tts_consumer.py`, `test_subagent_failure_notice.py`,
`test_task_card_destination_preflight.py`, `test_telegram_progress_literals.py`,
`test_turn_context.py` and `test_context_ref_expansion_runtime.py` under
`tests/gateway/`. These cover callback-only construction and inbound reference
expansion through real persisted routing. Also run
`tests/tui_gateway/test_tui_gateway_server.py` separately to distinguish its
background-worker timing failures from gateway routing regressions.
Run the role-hold lifecycle suite, topic tool suite, session and session-store suites,
and the attended model unit and turn-runner tests together with the gateway regression tests that exercise session limits, Telegram topic admission, reaped-session recovery, cron estop, fallback notices, reasoning flags, and status rehydration. Run `scripts/check_fork_patches.py --repo . --source-only` to verify trailer ownership.

## Retirement and rollback

Retire when an adopted upstream release provides equivalent attended model switching and disabled-path compatibility, or an upstream Telegram topic tool provides the same agent-callable contract. Remove the corresponding routing/tool implementation, tests, configuration and documentation, and this maintenance unit without changing the existing default routing path.
