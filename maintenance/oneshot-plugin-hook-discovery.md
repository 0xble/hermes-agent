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
checking the discovery flag. Every registry reader on the delivery path goes
through it: `invoke_hook`, `has_hook`, middleware, and the streaming hook
snapshot `iter_hook_callbacks`. The regression tests force the intermediate
state (`_discovered=True` while discovery is in flight), both with a stubbed
join and with a real worker thread that registers `pre_llm_call` late.

## Concurrent module eviction

Waiting for discovery is necessary but not sufficient: a plugin that failed to
import never registers its hook. Directory loading evicts the package's old
`sys.modules` entries before executing its `__init__.py`. Discovery and its
per-plugin deadline worker overlap main-thread startup imports and the
`cli-mcp-discovery` thread. Iterating the live module table can therefore raise
`RuntimeError: dictionary changed size during iteration`; the loader records
that exception and leaves an otherwise healthy plugin disabled. This produces
the same missing first-turn hook and injected context as an incomplete discovery
barrier.

`_evict_modules()` iterates `sys.modules.copy()` and removes each selected key
with `pop(name, None)`. The snapshot avoids unrelated import mutations, and
idempotent deletion tolerates another eviction after selection. A loader-local
lock would not protect against Python imports that do not acquire that lock.
The package and its dot-prefixed descendants are still removed; similarly
prefixed, unrelated packages are preserved. Initial load, failed-import cleanup,
and manager unload share this helper.

`tests/hermes_cli/test_plugins_loader_module_eviction.py` forces insertion during
iteration and deletion between selection and removal without timing sleeps. It
loads a real directory plugin, verifies that it stays enabled, and invokes its
registered `pre_llm_call` hook to verify the context canary survives.

Upstream `NousResearch/hermes-agent` main at
`dce1e9b37581dd62e480a9064dc04a709c2940d3` retains the live iteration and strict
deletion. Adopt an upstream equivalent rather than preserving a second fix.

## Verification and retirement

Verify `tests/hermes_cli/test_plugin_delivery_discovery.py`,
`tests/hermes_cli/test_plugins_loader_module_eviction.py`, and the plugin
provider discovery tests. The parity one-shot probe must report both the plugin
marker and injected canary under constrained Linux startup. Retire the relevant
parts of this patch when the upstream baseline guarantees both the discovery
barrier and mutation-safe module eviction.
