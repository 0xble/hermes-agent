# Oneshot plugin-hook discovery

Fork patch identity: `oneshot-plugin-hook-discovery-barrier`.

## Contract

A CLI one-shot turn must not invoke lifecycle hooks against a partially loaded
plugin registry. Startup may launch plugin discovery in a daemon thread, and
`PluginManager.discover_and_load()` marks its registry discovered before loading
plugin modules to prevent recursive discovery. Hook delivery must therefore
join an in-flight discovery worker before reading the registry.

## Failure and fix

The oneshot entrypoint starts background plugin discovery and then builds the
first turn. The delivery path treated `_discovered=True` as equivalent to
"discovery complete" and skipped `_join_background_discovery()`. Under startup
contention, `pre_llm_call` observed an empty hook registry, so the turn still
completed but the plugin marker and injected canary were absent.

`_delivery_manager()` joins the background worker before checking readiness. The
recursion guard (`_discovered`) is not readiness (`_discovery_complete`): a timeout
or re-entry during the sweep refuses partial delivery. A sibling foreground
sweep is also awaited through the existing discovery lock. Every registry reader
on the delivery path goes through this barrier: `invoke_hook`, `has_hook`,
middleware, and the streaming snapshot `iter_hook_callbacks`.

Regression workers are event-gated rather than sleep-gated. Concurrent independent
tool deliveries carry distinct `tool_call_id` values so this readiness proof does
not collide with the bounded callback gate. A same-call variant deliberately
reuses the identity and verifies that in-flight duplicate suppression remains in
place; production deduplication is unchanged.

## Verification and retirement

Verify `tests/hermes_cli/test_plugin_delivery_discovery.py` and the plugin
provider discovery tests. The parity one-shot probe must report both the plugin
marker and injected canary under constrained Linux startup. Retire this patch
when the fork's upstream baseline guarantees the same discovery barrier.
