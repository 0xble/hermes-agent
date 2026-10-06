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

`_delivery_manager()` now joins the background worker unconditionally before
checking the discovery flag. The original barrier still used the legacy 30-second
join cap, so a loaded runner could return after the cap with `_discovered=True`
but no hook registrations; the first oneshot request then completed without the
plugin marker or injected canary. The barrier now waits for the worker to finish
(the per-plugin loader deadline remains the bounded failure path). Every registry
reader on the delivery path goes through it: `invoke_hook`, `has_hook`, middleware,
and the streaming hook snapshot `iter_hook_callbacks`. The regression tests force
the intermediate state (`_discovered=True` while discovery is in flight), both
with a stubbed join and with a real worker thread that registers `pre_llm_call`
late, and cover the no-timeout join contract.

## Verification and retirement

Verify `tests/hermes_cli/test_plugin_delivery_discovery.py` and the plugin
provider discovery tests. The parity one-shot probe must report both the plugin
marker and injected canary under constrained Linux startup. Retire this patch
when the fork's upstream baseline guarantees the same discovery barrier.
