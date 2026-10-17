# Attended model switching

## Precedence

The gateway resolves a lane in this order: `--once`, explicit session
picks, temporary picks, active `model.attended`, then the channel/global
fallback. A session `/model reset` removes the persisted model pick; a
`/reasoning default` or `/reasoning reset` removes the persisted reasoning
pick. Plain picks remain temporary while attended switching is enabled and
expire after the idle window.

## Telegram topic model edits

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

The combined resolution is implemented in `gateway/run_turn.py` and
`gateway/run_config_loaders.py`; the topic commit path is
`gateway/telegram_topic_sessions.py` and `gateway/slash_commands_model.py`.
