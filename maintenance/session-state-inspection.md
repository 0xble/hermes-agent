# Typed Session-State Inspection

Load this unit when changing the typed `session_state` tool, its Telegram bundle registration, or the state.db command preview boundary.

## Required behavior

The opt-in `session_state` tool exposes only three bounded read-only actions: `resolve_topic`, `inspect_session`, and `inspect_delivery`. It is profile-scoped, uses the active Hermes state path, rejects SQL-shaped selectors, caps fields and serialized output, and does not create, migrate, or write `state.db`. Under multiplexed routing, delivery inspection reads the process-home shared ledger filtered by adapter profile, while session selectors resolve in the served profile's state. Delivery content is omitted unless explicitly requested and remains capped. Session transcript messages are never returned. `session_search` remains the owner of conversational recall.

Raw commands that target Hermes `state.db` are neutralized in progress previews without hiding unrelated SQLite commands.

## Provenance and patches

Fork patch identity: `session-state-inspection`.

The tool is registered in its own opt-in toolset and folded into the Telegram bundle, not the core toolset. Source surfaces are `tools/session_state_tool.py`, `toolsets.py`, `agent/display.py`, `tests/tools/test_session_state_tool.py`, `tests/agent/test_display.py`, and the shared-metrics tool/schema contract.

## Verification

Run `HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh tests/tools/test_session_state_tool.py tests/agent/test_display.py` and `scripts/run_tests.sh tests/hermes_cli/test_shared_metrics_efficiency.py`. The focused proof must cover real registry dispatch against temporary profiles, named-profile resolution without a bound session profile, multiplexed delivery reads from the process-home ledger, identical-looking session ids without cross-profile fallback, an absent state database without creation, topic and delivery reads, bounded output, SQL-shaped rejection, no migration during reads, and preview redaction.

## Retirement and rollback

Retire when a released upstream Hermes surface provides an equivalent typed, bounded, profile-scoped read-only route and preview boundary while preserving the same no-migration and no-transcript guarantees. Roll back by reverting the commits carrying `Fork-Patch: session-state-inspection`; no persisted data or schema migration is part of this patch.
