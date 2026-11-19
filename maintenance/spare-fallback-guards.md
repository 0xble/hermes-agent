# Spare fallback guards

This unit owns the shared admission and daily token protections for spare fallback upstreams used by delegated children.

## Guarantee

A delegated child can activate at most the configured number of routes for each spare upstream family across Hermes processes. The lease is released when the child exits or restores the primary and is removed after expiry or an orphaned process. API usage refreshes the lease; its one-hour TTL is only a backstop for a child stuck in a long tool call. A fallback route whose family has exhausted its local-day input + cache-read + output token budget is skipped, while a spare configured as the primary is never charged against this budget.

## Configuration

`delegation.spare_upstreams` defaults to `meta` and `xai`; `delegation.max_concurrent_on_spare` defaults to `2`. `fallback_budgets.meta` and `fallback_budgets.xai` default to `30_000_000` tokens/day and are tunable. An entry may set `upstream` explicitly when provider/model naming is ambiguous.

## Proof surface

The state file under `$HERMES_HOME/state` is guarded by an OS file lock and atomically rewritten. `tests/agent/test_spare_guards.py` covers cap enforcement and release, budget skipping, primary exclusion, and day rollover. `agent/spare_guards.py` resolves a route's upstream family through `hermes_cli.fallback_config.infer_upstream_family`, the resolver config validation uses.

Fork patch identity: `spare-fallback-guards`.

Contract tests: `tests/agent/test_spare_guards.py`.
