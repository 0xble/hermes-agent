# Oneshot plugin-hook discovery

Fork patch identity: `oneshot-plugin-hook-discovery-barrier`.

## Contract

A CLI one-shot turn must not invoke lifecycle hooks against a partially loaded
plugin registry. Startup may launch plugin discovery in a daemon thread, and
`PluginManager.discover_and_load()` marks its registry discovered before loading
plugin modules to prevent recursive discovery. Hook, middleware, streaming
snapshot, and prompt-section delivery must therefore use one completion barrier
before reading the registry.

## Failure and fix

The oneshot entrypoint starts background plugin discovery and then builds the
first turn. The delivery path treated `_discovered=True` as equivalent to
"discovery complete" and skipped the barrier. Under startup contention,
`pre_llm_call` observed an empty hook registry, so the turn still completed but
the plugin marker and injected canary were absent.

The barrier now uses one module-level `_discovery_done` event and a published
`_discovery_outcome` (`ok` or `failed`). A background worker assigns its outcome
and sets the event only in its completion `finally`; synchronous discovery uses
the same publication path. Incomplete state is derived from an active background
worker whose event is not set—there is no mutable incomplete flag to clear and
no completion-vs-timeout race. A timed-out first wait logs one ERROR naming the
active plugin. A write-once timeout bit changes later waits to zero, but does
not affect correctness: a late event publication automatically reopens delivery.

If the worker fails, its published `failed` outcome keeps plugin hooks,
middleware, streaming callback snapshots, and prompt sections closed. Delivery
logs the fail-closed warning once and never retries discovery implicitly. Every
consumer routes through `_delivery_manager()`; the execution-middleware chain
also handles its `None` result by calling the terminal operation without plugin
middleware. Registration and plugin-load-worker paths retain their existing
no-op behavior.

The current plugin field is set around each manifest's dependency check,
configuration validation, and load/register operation, while filesystem scans
remain reported as unknown. The per-plugin loader deadline and abandoned-loader
cap remain unchanged.

## Verification and retirement

Verify `tests/hermes_cli/test_plugin_delivery_discovery.py` and the plugin
load-timeout tests. The parity one-shot probe must report both the plugin marker
and injected canary under constrained Linux startup. The regression suite covers
pre-command delivery during in-flight discovery, worker failure, timeout races,
late completion reopening delivery, bounded repeat waits, and the normal path.

Retire this patch when the fork's upstream baseline guarantees the same bounded
discovery barrier and outcome-based fail-closed delivery state.
