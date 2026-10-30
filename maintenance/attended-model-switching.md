# Attended model switching

## Required behavior

When `model.attended` is configured, genuine user activity in a topic may route the next gateway request to the attended model and reasoning effort until the configured idle window expires. New sessions start on the attended model by default when `model.attended.start_new_sessions` is true; their first route is silent. Relays, wakeups, reactions, edits, and internal messages do not reset the user-activity clock. Explicit session and one-turn model choices retain their precedence. Idle expiry is committed only at a real API-request boundary, and only when both the genuine-activity window and the session's API-call quiet window have elapsed. A session opened by automation, such as a topic split or relay, has no genuine-activity stamp yet, so it stays attended until the user's first message there starts the idle window. If the request is already at the default model's compression threshold, Hermes keeps the attended route and does not switch or compact for this purpose.

The legacy `model.compact_before_switch_tokens` key is still accepted for compatibility but is deprecated and ignored. Normal per-model preflight compression is the only compaction path.

When the attended configuration is unset or disabled, all attended activity tracking, route resolution, notices, compaction, and model-switch hooks are no-ops. Existing gateway call shapes and default model behavior remain unchanged.

## Switch Cards

`model.attended.notices` (boolean, default true) controls the persistent "⇄ Switched to {model} while you're active." and "⇄ Back to {model} after Nm idle." cards. When it is false, attended switching and switch-back still happen at the same boundaries, but no card is staged for a queued turn or posted mid-turn. A non-boolean value logs a warning and keeps the cards. The model-pick expiry notice is not a switch card and is unaffected.

## Attended Session Pin

`/attend` pins this session to the live attended configuration permanently,
until `/attend off` or a session boundary. `/attend 30m`, `/attend 2h`,
`/attend 1h30m`, and `/attend 1d` pin it for a positive duration.
`/attend status` reports whether the pin is permanent, timed, or inactive and
shows the current effective route. Timed confirmations and status include the
release date and time in the owning profile's local timezone, with AM/PM and
the timezone abbreviation. Status also shows the remaining duration. These
forms dispatch while a turn is running without interrupting it.

Durations use the shared `parse_duration_seconds` parser, including seconds,
minutes, hours, days, weeks, fractional units such as `1.5h`, and compact
combinations such as `1h30m`. `/attend` requires a unit. Zero, negative,
non-finite, malformed, and out-of-range durations return usage without mutating
session state. Examples of invalid input are `/attend 0m`, `/attend -2h`,
`/attend NaN`, `/attend 30`, and `/attend tomorrow`. Re-pinning with a duration
replaces the deadline with now plus that duration. Re-pinning with plain
`/attend` removes the deadline and makes the pin permanent.

The pin is a persisted `attended_pinned` boolean plus the nullable
`attended_pin_until` Unix timestamp on the session routing entry, not a concrete
model override, and survives gateway restart. A legacy true boolean without a
deadline remains a permanent pin. Existing explicit `/model` picks and held
roles retain precedence.

Active pinned sessions have no attended idle expiry or switch-back notice.
Legacy idle expiry of temporary model and reasoning picks is also suspended,
while an explicit model-pick TTL still expires on its own deadline. At or after
the timed deadline, pure resolution treats the pin as inactive and uses
`max(last_user_activity_at, attended_pin_until)` as the activity floor. Thus the
normal idle window starts at the deadline, not at the last message before the
pin, and subsequent genuine activity can extend it. Expiry performs no routing
persistence write and no immediate model switch. The historical boolean and
deadline remain until a re-pin, explicit release, or session boundary. The
existing API-call quiet window and destination compression guard still apply
before the next real API-request boundary can switch back.

Unpinning atomically resets `last_user_activity_at` to now, restoring the
configured idle window without reverting immediately. Releasing an
already-unpinned or expired session reports its current status without
restarting that idle window. The pin and deadline survive non-boundary session
repoints, including topic-binding repair, delegation completion, and CLI
handoff. Session reset, `/resume`, and `/branch` start unpinned conversations
with no deadline.

Run `tests/gateway/test_attend_command.py` alongside the attended routing, busy
activity, API-boundary, and topic readback tests. `expiry_time` returns no deadline
for a permanent pin and returns the deadline activity floor plus the idle window
for a timed pin. `/attend` commands are
excluded from generic receipt-activity stamping, so inspections and rejected
input cannot extend the idle window. The synchronous and asynchronous turn-expiry
paths and inspection path all share the pinned activity predicate.

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
pick. With `model.pick_ttl_hours` unset, plain picks keep their existing behavior:
temporary with attended routing (cleared after its idle window), otherwise
session-scoped. A positive finite `model.pick_ttl_hours`, for example `12`,
opts plain model picks into that many wall-clock hours instead of idle expiry,
regardless of whether attended routing is enabled. Zero, negative, non-finite
and invalid values leave the policy disabled. No config is changed automatically.
The confirmation states when the pick expires, in hours, only for timed picks.
The deadline is sanitized and saved inside the existing session override so
restart, idle gaps and `--once` restoration do not extend it. At or after the
deadline the next turn clears the pick under the route lock and returns to the
current held role, attended, channel or global route. The reply carries a
one-line expiry notice naming the resolved model. `--session`, `--once`,
`--global`, topic-tool edits and temporary reasoning idle expiry are unchanged.
Temporary picks preserve a named hold and expire back to it, just as `--once`
preserves it. `/model reset` immediately clears a timed pick.

Without slash-admin gating, the restricted env path for `/model reset --all`
accepts a human DM principal listed in the platform or gateway env allowlist.
It reuses canonical principal matching and platform-only adapter-resolved IDs.
Role, relay, upstream adapter, bot and pairing-store admission alone do not
satisfy this path. Pairing approvals mirrored into a configured env allowlist
count as listed principals after reload, indistinguishable from manually added
IDs. A wildcard in either env list, or the platform's allow-all flag, disables
this env path even for an explicitly listed owner.

Explicit slash-admin gating remains authoritative. The pre-existing adapter
`allow_from` fallback remains unchanged for compatibility. Its wildcard admits
any DM sender, including bots and non-owners, independently of the env path.

Role assignment is bound to a turn, not to its starting config snapshot. Busy
topic edits and bulk conversions take effect next turn, even before the current
turn's first API request. Durable role and concrete picks hydrate under their
shared route lock before gateway turn assembly captures the assignment
synchronously and supplies it to TurnRunner. Worker initialization and request
callbacks use that same assignment. Callback-only construction does not read routing
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
expiry notice. A fresh session starts attended without a switch-in card. The
new-session marker lasts until inbound turn finalization, including failed and
interrupted turns. Finalization clears both the admission key and the resolved
session key, because Telegram topic recovery can change the thread identity.
Preparatory route probes, such as image-mode selection and
vision enrichment, must not consume first-turn silence. Later genuine
switch-ins still announce the configured idle window.
Idle expiry emits a separate back-to-default line and leads with the `⇄` symbol.
For example, with a `gpt-6.1-sol` default and a 15 minute window:

```text
⇄ Back to GPT-6.1 Sol after 15m idle.
```

## Provenance and patch

Fork patch identities: `attended-model-switching`, `gateway-telegram-topic-tool`, `live-model-preset-roles`, `model-pick-expiry`.

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

Run `tests/gateway/test_attended_session_opening.py` for new user-opened text
and photo topics, `telegram_topic`-created topics, preparatory route probes,
first-request silence, later switch-in notices, and genuine-activity provenance.
The tests enter through `_handle_message` and assert marker cleanup after success,
exceptions, and task cancellation, including recovery from a lobby key to a topic.

Run `tests/gateway/test_model_pick_expiry.py` for disabled-by-default policy,
positive TTLs with attended routing on and off, command confirmation, unchanged
flags, persisted restart hydration, exact-deadline routing and notices,
repicking, stale expiry, one-turn restoration, immediate reset and independent
reasoning idle expiry. Pair it with `test_attended_model.py`,
`test_session_model_override_persistence.py` and the model-command regressions.

Run `tests/gateway/test_attended_busy_activity.py` when changing busy admission.
Genuine replies must refresh activity at receipt on both runner and adapter paths,
including Telegram grace queueing and foreign-owner handover, without counting automation or refreshing again when the same event drains. The old turn owner must observe the successor's durable activity at the next API boundary, but never adopt a replacement session's timestamp. Receipt tasks are retained until completion and capture the timestamp before worker hops. An in-flight receipt is joined during drain, and out-of-order workers cannot move the local activity clock backward. Durable refresh is opt-in for the off-loop routing snapshot and committed request boundary, not every cached getter.

Shutdown and drain-deferred follow-ups preserve `_attended_activity_received_at` through the shared spool serializer and recovery builder. Genuine input captures its receipt at common admission before startup/reconnect restore, compression deferral, or orphan FIFO rescue can buffer it. Restore queues retain only receipt metadata. Durable bookkeeping waits for the existing user-authorization and bot-admission checks on drain. Bot-origin input never counts as genuine activity, even when bot admission permits it to run. A recovered follow-up may publish only that original receipt, never its reconstruction or replay time. Recovery treats non-numeric, boolean, and non-finite receipts as missing. Legacy spools without the receipt do not count as fresh activity, because their ordering `ts` may instead be a shutdown flush time. Startup auto-resume remains internal and cannot reset activity. The busy-activity regressions round-trip both spool kinds through durable activity writes, including arrivals interrupted before their receipt write completes.

Activity ordering compares instants, not naive wall times. The repeated DST hour
must advance both activity and `updated_at`, survive durable reload with its fold
intact even when a turn marker updates the row, and reject older receipts.
Scheduled receipt bookkeeping retries once after an ordinary exception even
when steering consumes the event. Cancellation propagates immediately, and
disabled routing or missing sessions are terminal, not retryable failures.
A concurrent drain joins that same task, and both attempts retain the original
arrival time. Store activity publishes only after persistence succeeds, so a
failed write cannot make its retry look like an already-recorded receipt. Run
`tests/gateway/test_session_store_lock_io.py` for refresh reads outside the store
lock and concurrent activity or session replacement during those reads.

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
