# State.db prune lock batching

## Scope

Automatic session retention pruning is destructive background maintenance. It must not spend its daily throttle on a daytime run or hold SQLite's WAL write lock while deleting an unbounded session/message/FTS set.

## Contract

- Gateway and CLI automatic pruning run only when `maintenance.deletion_window` is open. Outside the window they return before touching the state database or its throttle markers.
- Retention pruning selects candidates read-only, then rechecks each bounded batch inside its own idempotent write transaction. Active write guards protect the complete compression-descendant lineage, not only the rows in the current batch.
- Each batch commits before the next batch begins and yields briefly so foreground writers can acquire the lock. Unreferenced system prompts are also removed in bounded transactions.
- `_execute_write_marked` emits a warning when one callback-plus-commit holds the lock for several seconds, including the callback name and process id. On macOS, the lock-owner diagnostic says `/proc/locks` is unavailable rather than claiming that a holder released just before the deadline.

## Source surfaces

`gateway/run.py`, `hermes_cli/cli_auto_maintenance.py`, `hermes_state_maintenance.py`, `hermes_state.py`, `hermes_state_lockowners.py`, and their focused tests.

## Focused regression

```text
uv run --with pytest --with pytest-asyncio pytest -q tests/gateway/test_state_db_maintenance_window.py tests/hermes_cli/test_session_vacuum_config.py tests/hermes_state/test_hermes_state.py tests/hermes_state/test_write_lock_owner_attribution.py
```

## Retirement

Remove this patch when upstream retains the deletion-window gate, bounded automatic prune transactions with guard-safe lineage handling, and cross-platform long-lock diagnostics.

## Rollback

Revert the commit carrying `Fork-Patch: state-prune-lock-batching`.
