# Intentional Message Reactions

## Required Behavior

The existing `react_to_message` tool can answer the actual gateway triggering
message through its live adapter's public reaction operation on the gateway event
loop. Only adapter-confirmed delivery to that execution's inbound target produces
a reaction acknowledgement. Queued messages bind their own message, sender,
workspace and source transport. A later execution starts without receipts.
Runtime profiles and receiving-bot profiles may differ, so deliberate reactions
reuse the source delivery resolver rather than guessing the active profile's bot.

A completed reaction-only response may be blank, `NO_REPLY` or `[SILENT]` without
text fallback. Failed, partial, interrupted, other-target and unacknowledged turns
retain existing recovery. Human-ingress policy, queued terminal authority, goal
notice visibility, substantive finalization and automatic status-reaction settings
are unchanged. Desktop reactions retain their opt-in and persisted-message path.
An explicit unknown Slack workspace fails closed, never borrowing the primary
workspace client. Explicit tool selections and disabled toolsets remain authoritative, including
registered plugin platforms. The private unregistered `send_message` helper stays
unregistered.

## Provenance And Adaptation

Fork patch identity: `intentional-message-reactions`.
Feature-only adaptation from reviewed reference
`240993e186e70e36878cdcf88ec7e09c0cefefcf`, relative to
`0c9c66de13bdd258f75e2eae87ab5f98cda4429f`, onto Meridian source
`417de3eea2ef23ff4ca7df928d511e4267453eb4`.
Do not replace this lineage's response-recovery or gateway runtime with modern
silence-reprompt, answer-in-reasoning or session-disabled-toolsets infrastructure.
Its existing agent denylist freezes the desktop opt-in. Its existing final-response
and queued-delivery boundaries consume the per-execution result flag.

The reference's copied empty-pin test assumed newer Feishu native recovery.
This lineage deliberately restores Feishu native toolsets for an empty saved pin
(`hermes_cli/tools_config.py`, refactor `5d4b97939ed66` and its predecessor).
The corrected test asserts the actual feature contract: an explicit empty pin
never gains reactions. Feishu native recovery is not changed. The original
behavioral RED is retained in the source-candidate handoff evidence.

## Verification

Run the repository wrapper, not a borrowed environment:

```bash
scripts/run_tests.sh -j 2 tests/agent/test_reaction_only_final.py \
  tests/gateway/test_adapter_reactions.py tests/gateway/test_reaction_only_turn.py \
  tests/gateway/test_reaction_routing.py tests/gateway/test_reaction_toolsets.py \
  tests/tools/test_gateway_reactions.py tests/tools/test_send_message_react.py \
  tests/hermes_cli/test_tools_config.py
```

Also retain `tests/tools/test_react_to_message_tool.py`,
`tests/gateway/test_gateway_silence_tokens.py`,
`tests/gateway/test_slack_api_human_senders.py`,
`tests/gateway/test_goal_notices_config.py` and the existing finalization,
profile-routing and adapter-reaction proof surfaces. Routing tests exercise the
real registered tool, turn execution, adapter resolver and Slack workspace client
selection without making live sends. They assert behavior, not a receipt tuple.

## Retirement And Rollback

Retire when the selected qualified release passes these contracts without the
local patch. Rollback reverts this identity's source and tests, with no persistent
receipt data or live configuration migration. Source qualification and company
activation remain separate, and this unit grants no activation authority.
