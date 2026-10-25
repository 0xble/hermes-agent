# Session reasoning persistence

## Required behavior

A messaging-gateway session's `/reasoning` pick (including `/model X --reasoning <level>`) survives a gateway restart the same way the session `/model` pin does. `_set_session_reasoning_override` writes the pick through to the routing entry (`SessionEntry.reasoning_override` in `sessions.json`/state.db routing). After a restart, `_session_reasoning_override` lazily rehydrates it on first resolution. Live in-memory state wins over the persisted value. Persisted values pass through `sanitize_reasoning_override` (canonical `parse_reasoning_effort` shape, `{"enabled": False}` or `{"enabled": True, "effort": str}`), so extra keys never reach disk and malformed values load as absent. `/reasoning reset`, `/reasoning --global`, `/new`, auto-reset and `/resume` clear the persisted pick. `switch_session` carries it alongside the model pin.

## Provenance and patch

Fork patch identity: `gateway-session-reasoning-persistence`.

On base `aa0870b0832`, `/reasoning` session picks lived only in `SessionState.conversation.reasoning_override`, so every restart silently dropped sessions back to `agent.reasoning_effort` while the paired `/model` pin survived. The regression in `tests/gateway/test_session_reasoning_override_persistence.py` fails on that base (restart resolves `high`, not `xhigh`).

Upstream [PR #98901](https://github.com/NousResearch/hermes-agent/pull/98901) (open, unreleased, unreviewed) fixes the same gateway gap with the same storage shape, plus a TUI-gateway leg this fork does not need. The fork adaptation differs only in sanitization: it reuses `parse_reasoning_effort`, so an effort-less `{"enabled": True}` loads as absent rather than as an override.

## Verification

`scripts/run_tests.sh tests/gateway/test_session_reasoning_override_persistence.py tests/gateway/test_model_command_reasoning_flag.py tests/gateway/test_running_agent_session_toggles.py tests/gateway/test_reasoning_command.py`.

## Retirement and rollback

Retire when an adopted upstream release persists and rehydrates the session reasoning pick across messaging-gateway restarts with clears on reset, `/new` and `/resume`. Revert the scoped changes in `gateway/session.py`, `gateway/run_config_loaders.py`, `gateway/slash_commands_session.py`, the test, this file and its index row. A persisted `reasoning_override` key left in routing rows is ignored by builds without the field, so rollback needs no state migration.
