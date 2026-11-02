# Native Telegram topic tool

## Required behavior

An agent in a Telegram DM session can create or edit a DM topic as its own Hermes session through one tool, `telegram_topic`, instead of replaying `/title`, `/model`, `/reasoning` and icon edits as the user through the MTProto CLI.

- `create` validates the name (session-title limit), icon (Telegram's topic icon set), reasoning effort and model (resolved exactly as `/model` resolves it) before calling Telegram, so an invalid request never leaves an orphan topic. A model that `/model` would ask the user to confirm (cost, data-training tier, large-context switch, through the shared `_model_selection_warning`) is refused, because an agent cannot give that confirmation; the user switches with `/model` in the topic. It then creates the topic in the caller's chat, binds a new session to it, stores the title at user authority through `set_session_title_in_lineage`, records the applied icon as `manual` so automatic renames keep it, commits model and reasoning through the `/model --session` and `/reasoning` commits, and optionally delivers an opening brief. The brief is shown as a short title, objective and explicit next-action preview, with the complete original in a closed `<details>` toggle and a small italic origin link below it. The unmodified origin-plus-brief input is admitted as the session's first turn with `internal=True` and `allow_gateway_control=False`, so text starting with `/` is never run as a command. The turn is anchored on the shown brief's message, as a typed message anchors its own turn. Internal events skip the `hermes pause` gate, so a paused Hermes sends no brief. A brief Telegram did not accept is not admitted, and the call fails naming the set-up topic, its session and the relay address to send the brief to. It returns `thread_id`, `session_id` and the relay address `hermes:<profile>/<session_id>`, where the profile falls back to the gateway's own when the source names none. A new topic's first turn gets no "model was just switched" note.
- `edit` changes a topic's name or icon (an icon-only edit omits the name so Telegram keeps the visible one), retitles the bound session, and changes its model or reasoning. It resolves the topic's session the way a turn there does (`_hmwa_heal_telegram_topic_binding`): an existing binding wins over the route and is healed to its compression tip, and a topic with no binding is bound to the session it routes to. That finishes a `create` whose setup failed after Telegram made the topic and never repoints a bound topic. A settings-only edit names no Telegram call, so a `thread_id` Hermes has no binding or route for is refused. A model change on a session that is mid-turn is queued as the durable next-turn override without swapping the running agent; an idle model change applies for the next turn. Reasoning changes update the running agent's next request, or the next turn when idle. `reasoning=default`/`reset` clears the session override. Typed `/model` keeps its busy refusal, and model-only changes resolve against the session's persisted route even before its first turn after a restart.
- A create request requires an explicit icon when Telegram returns a non-empty native icon set. A missing or unsupported icon lists the available choices before any topic is created. If the set is empty or cannot be fetched, creation proceeds without an icon and returns a warning. The result reports the emoji actually applied, or `null` when no icon was applied. Edit keeps the icon optional.
- A setup failure after Telegram created the topic names the topic and the full `edit` call that finishes it.
- Source copies use `replace_source`, so the wire-invisible routing provenance (receiving bot, identity) survives and a multiplexed shared-bot turn acts through the bot that received it.
- Results keep `model` and `provider` as requested picks, or `null` when unchanged. `effective_model`, `effective_provider` and `effective_effort` describe the destination's next-turn route through detached snapshots shared with turn setup, including session picks, channel overrides, attended routing and the owning profile's config. Config reads and cold provider credential hydration stay off the gateway loop. Inspection does not hydrate live conversation state, expire durable picks, consume notices or update last-resolved-model caches. An undeclared reasoning effort reports the normal `medium` default. If optional readback fails after successful creation or editing, the operation still returns its successful result with all `effective_*` fields `null` and a short warning, so callers do not retry an already-applied mutation.

The tool owns no state. Telegram owns the remote topic; `gateway/telegram_topic_sessions.py` owns topic and session setup and is shared with `/topic edit`; `GatewayModelCommandsMixin.resolve_session_model_selection` and `commit_session_model_selection` expose the `/model` resolve and commit without a slash event; the routing entry persists model and reasoning picks across restarts. The work runs on the gateway event loop through `_dispatch_on_gateway_loop`, because the Bot API client, the model-switch lock and turn admission belong to it.

The tool lives in its own `telegram_topic` toolset, included only in `hermes-telegram`, so it reaches Telegram sessions (including saved explicit toolset lists) and no other platform. Its `check_fn` checks only that a gateway is running. It must not check connected adapters: the boot warm-up builds tool schemas before any adapter connects, and `get_tool_definitions` memoizes by toolset selection, so such a check dropped the tool from every Telegram session until the next restart. Each call uses the calling profile's own adapter through `_delivery_adapter_for`, which fails closed when that profile has no connected Telegram adapter.

## Provenance and patch

Fork patch identity: `gateway-telegram-topic-tool`.

Method decision: core plus a thin native tool. The behavior needs the gateway's session store, topic bindings, title authority, model and reasoning commits and turn admission; no plugin API exposes them, so a plugin or wrapper would import private gateway internals or replay slash commands as the user. Hermes's Footprint Ladder rung is a service-gated tool on a platform toolset, like `discord`.

No upstream equivalent exists. Upstream PRs adding Telegram topic creation were not found when this was written (2026-10-09).

Required-create-icon preflight: upstream `NousResearch/hermes-agent` guidance pinned at `c57331677d7fc46298a7159cf3a650853f0966c5` (2026-10-10). Root guidance directs changes to existing owners and requires tests; root contribution and subsystem guidance files were absent at that revision. Related upstream PRs [#130641](https://github.com/NousResearch/hermes-agent/pull/130641) and [#119907](https://github.com/NousResearch/hermes-agent/pull/119907) concern automatic rename icon selection, not an agent-callable create contract. This fix stays in the existing topic-session owner, retains native-set validation, fails open only when the catalog is unavailable, and does not change auto-titling. Brian explicitly chose caller-selected icons over extending the deterministic fallback.

Icon matching uses `agent.topic_icons.normalize_emoji` so catalog presentation
selectors do not change identity. Unsupported identities still fail before any
Telegram mutation, with the valid catalog and a nearest-spelling suggestion;
suggestions never select an icon automatically. `tests/gateway/test_topic_icon_recovery.py`
pins this boundary for create and edit. Retain it when adopting upstream topic
work; ZWJ sequences and skin tones must not collapse to a different icon.

## Dependencies

Consumes `gateway-session-reasoning-persistence` for reasoning picks that survive restarts. Do not add a second persistence path.

## Opening Brief Presentation

`gateway/telegram_topic_sessions.py::_opening_brief_display` shapes only new-topic delivery. It parses the caller's Markdown before Telegram transport normalization, isolates an explicit heading as a bold title paragraph, bounds the objective and explicit `Next action:` excerpt, and retains the full brief behind `<details>` on rich delivery. Unstructured briefs, including wrapped prose, use the topic name as the title. A leading plain or emphasized line equal to that name is the title, not an objective. An explicit next action is shown once, not also repeated as the objective. Soft or hard line breaks after the action label or inside its sentence are joined before the excerpt is bounded. No next action is invented when the caller supplies none. Quotes, file paths, instructions and constraints remain in the complete brief. Literal or nested disclosure tags are escaped so they cannot close the containing toggle. The internal event keeps the original origin-plus-brief text, routing and message anchor. Existing session transcripts are never rewritten.

`_deliver_brief` supplies the original origin-plus-brief as `rich_fallback_content` send metadata. The Telegram adapter selects it at the actual legacy boundary, after rich eligibility and any safe permanent-refusal fallback. Disabled or unavailable rich delivery, CJK/math exclusions and size limits therefore show the original brief once, with no generated disclosure tags or CommonMark preview escapes. Ambiguous rich send failures still fail without a legacy resend or turn admission, avoiding duplicates. Do not decide fallback solely in the topic caller, because a rich endpoint can refuse an eligible payload during delivery.

The reproduced failure was a relay-style `title\nobjective\n---` envelope, which Markdown parses as a multiline setext heading, not a title followed by prose and a divider. `_rich_materialize_prose_paragraphs` then replaces the origin-to-title blank line with a hard-break spacer, pulling the origin into that same heading. The regression drives the real adapter and parses its outgoing rich payload, so changing the general newline implementation cannot silently reintroduce oversized preview headings. This is a topic-presentation fix, not a global change to valid Markdown headings.

## Verification

Contract tests: `tests/gateway/test_topic_icon_recovery.py`,
  `tests/gateway/test_telegram_topic_briefs.py`,
  `tests/gateway/test_telegram_topic_tool.py`,
  `tests/gateway/test_topic_model_readback.py`,
  `tests/gateway/test_telegram_topic_edit.py`,
  `tests/gateway/test_session_reasoning_override_persistence.py`

`scripts/run_tests.sh tests/gateway/test_telegram_topic_briefs.py tests/gateway/test_telegram_topic_tool.py tests/gateway/test_topic_model_readback.py tests/gateway/test_telegram_topic_mode.py tests/gateway/test_telegram_topic_edit.py tests/gateway/test_telegram_rich_messages.py tests/gateway/test_session_reasoning_override_persistence.py tests/gateway/test_model_command_reasoning_flag.py tests/hermes_cli/test_tools_config.py`.

The topic-tool tests drive a real `SessionStore` and `SessionDB` and fake only Telegram and model resolution. They cover the restart path: a fresh runner over the same store rehydrates the chosen model and reasoning.

Live check: create one topic with the tool, read back its binding, title, icon, overrides and brief, relay to it, restart the gateway, read the overrides back again, then close the topic.

## Retirement and rollback

Retire if upstream ships an equivalent agent-callable topic tool. Roll back by reverting the tool, `gateway/telegram_topic_sessions.py`, the two `GatewayModelCommandsMixin` methods, the `/topic edit` delegation, the toolset entries, docs, tests, this file and its index row. Topics and sessions it created stay ordinary topic sessions, so rollback needs no state migration.
