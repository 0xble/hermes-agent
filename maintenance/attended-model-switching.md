# Attended model switching

## Required behavior

When `model.attended` is configured, genuine user activity in a topic may route the next gateway request to the attended model and reasoning effort until the configured idle window expires. Relays, wakeups, reactions, edits, and internal messages do not reset the user-activity clock. Explicit session and one-turn model choices retain their precedence.

When the attended configuration is unset or disabled, all attended activity tracking, route resolution, notices, compaction, and model-switch hooks are no-ops. Existing gateway call shapes and default model behavior remain unchanged.

## Precedence and Telegram topic edits

The gateway resolves a lane in this order: `--once`, explicit session picks,
temporary picks, active `model.attended`, then the channel/global fallback. A
session `/model reset` removes the persisted model pick; a `/reasoning
default` or `/reasoning reset` removes the persisted reasoning pick. Plain
picks remain temporary while attended switching is enabled and expire after the
idle window.

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

## Provenance and patch

Fork patch identities: `attended-model-switching` and
`gateway-telegram-topic-tool`.

The attended feature adds per-topic attended routing, idle expiry, persisted activity timestamps, and route re-evaluation before every API call. Keep its disabled path inert so users without the new keys receive the pre-feature behavior exactly. The topic tool commits through the gateway's existing model and reasoning persistence paths rather than adding a second store.

The combined resolution is implemented in `gateway/run_turn.py` and
`gateway/run_config_loaders.py`; the topic commit path is
`gateway/telegram_topic_sessions.py` and `gateway/slash_commands_model.py`.
This is a fork-only gateway feature that adds per-topic attended routing, idle
expiry, persisted activity timestamps, and route re-evaluation before every
API call. A new genuine user message switches in at the next API call; idle
expiry switches back to the default at the next API call, including during a
long autonomous turn. An active fallback is never undone. Keep its disabled
path inert so users without the new keys receive the pre-feature behavior
exactly.

## Verification

Run the attended model unit and turn-runner tests together with the gateway regression tests that exercise session limits, Telegram topic admission, reaped-session recovery, cron estop, fallback notices, reasoning flags, and status rehydration. Run `scripts/check_fork_patches.py --repo . --source-only` to verify trailer ownership.

## Retirement and rollback

Retire when an adopted upstream release provides equivalent attended model switching and disabled-path compatibility, or an upstream Telegram topic tool provides the same agent-callable contract. Remove the corresponding routing/tool implementation, tests, configuration and documentation, and this maintenance unit without changing the existing default routing path.
