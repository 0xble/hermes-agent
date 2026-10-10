# Native Telegram topic tool

## Required behavior

An agent in a Telegram DM session can create or edit a DM topic as its own Hermes session through one tool, `telegram_topic`, instead of replaying `/title`, `/model`, `/reasoning` and icon edits as the user through the MTProto CLI.

- `create` validates the name (session-title limit), icon (Telegram's topic icon set), reasoning effort and model (resolved exactly as `/model` resolves it) before calling Telegram, so an invalid request never leaves an orphan topic. A model that `/model` would ask the user to confirm (cost, data-training tier, large-context switch, through the shared `_model_selection_warning`) is refused, because an agent cannot give that confirmation; the user switches with `/model` in the topic. It then creates the topic in the caller's chat, binds a new session to it, stores the title at user authority through `set_session_title_in_lineage`, records an explicit icon as `manual` so automatic renames keep it (without one it uses the same pick an automatic rename would make), commits model and reasoning through the `/model --session` and `/reasoning` commits, and optionally delivers an opening brief. The brief is shown in the topic and admitted as the session's first turn with `internal=True` and `allow_gateway_control=False`, so text starting with `/` is never run as a command. The turn is anchored on the shown brief's message, as a typed message anchors its own turn. Internal events skip the `hermes pause` gate, so a paused Hermes sends no brief. A brief Telegram did not accept is not admitted, and the call fails naming the set-up topic, its session and the relay address to send the brief to. It returns `thread_id`, `session_id` and the relay address `hermes:<profile>/<session_id>`, where the profile falls back to the gateway's own when the source names none. A new topic's first turn gets no "model was just switched" note.
- `edit` changes a topic's name or icon (an icon-only edit omits the name so Telegram keeps the visible one), retitles the bound session, and changes its model or reasoning. It resolves the topic's session the way a turn there does (`_hmwa_heal_telegram_topic_binding`): an existing binding wins over the route and is healed to its compression tip, and a topic with no binding is bound to the session it routes to. That finishes a `create` whose setup failed after Telegram made the topic and never repoints a bound topic. A settings-only edit names no Telegram call, so a `thread_id` Hermes has no binding or route for is refused. A model change on a session that is mid-turn is refused, re-checked under the `/model` lock at commit, and a model-only change resolves against the session's persisted route even before its first turn after a restart.
- A setup failure after Telegram created the topic names the topic and the full `edit` call that finishes it.
- Source copies use `replace_source`, so the wire-invisible routing provenance (receiving bot, identity) survives and a multiplexed shared-bot turn acts through the bot that received it.

The tool owns no state. Telegram owns the remote topic; `gateway/telegram_topic_sessions.py` owns topic and session setup and is shared with `/topic edit`; `GatewayModelCommandsMixin.resolve_session_model_selection` and `commit_session_model_selection` expose the `/model` resolve and commit without a slash event; the routing entry persists model and reasoning picks across restarts. The work runs on the gateway event loop through `_dispatch_on_gateway_loop`, because the Bot API client, the model-switch lock and turn admission belong to it.

The tool lives in its own `telegram_topic` toolset, included only in `hermes-telegram`, so it reaches Telegram sessions (including saved explicit toolset lists) and no other platform. Its `check_fn` checks only that a gateway is running. It must not check connected adapters: the boot warm-up builds tool schemas before any adapter connects, and `get_tool_definitions` memoizes by toolset selection, so such a check dropped the tool from every Telegram session until the next restart. Each call uses the calling profile's own adapter through `_delivery_adapter_for`, which fails closed when that profile has no connected Telegram adapter.

## Provenance and patch

Fork patch identity: `gateway-telegram-topic-tool`.

Method decision: core plus a thin native tool. The behavior needs the gateway's session store, topic bindings, title authority, model and reasoning commits and turn admission; no plugin API exposes them, so a plugin or wrapper would import private gateway internals or replay slash commands as the user. Hermes's Footprint Ladder rung is a service-gated tool on a platform toolset, like `discord`.

No upstream equivalent exists. Upstream PRs adding Telegram topic creation were not found when this was written (2026-10-09).

## Dependencies

Consumes `gateway-session-reasoning-persistence` for reasoning picks that survive restarts. Do not add a second persistence path.

## Verification

`scripts/run_tests.sh tests/gateway/test_telegram_topic_tool.py tests/gateway/test_telegram_topic_edit.py tests/gateway/test_session_reasoning_override_persistence.py tests/gateway/test_model_command_reasoning_flag.py tests/hermes_cli/test_tools_config.py`.

The topic-tool tests drive a real `SessionStore` and `SessionDB` and fake only Telegram and model resolution. They cover the restart path: a fresh runner over the same store rehydrates the chosen model and reasoning.

Live check: create one topic with the tool, read back its binding, title, icon, overrides and brief, relay to it, restart the gateway, read the overrides back again, then close the topic.

## Retirement and rollback

Retire if upstream ships an equivalent agent-callable topic tool. Roll back by reverting the tool, `gateway/telegram_topic_sessions.py`, the two `GatewayModelCommandsMixin` methods, the `/topic edit` delegation, the toolset entries, docs, tests, this file and its index row. Topics and sessions it created stay ordinary topic sessions, so rollback needs no state migration.
