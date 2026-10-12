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
sweep is also awaited through the existing discovery lock. The barrier helpers live in
`hermes_cli/plugins_dispatch.py` and are re-exported by the facade. Every registry reader
on the delivery path goes through this barrier: `invoke_hook`, `has_hook`,
middleware, and the streaming snapshot `iter_hook_callbacks`.

Async hook delivery snapshots a complete registry under a nonblocking sweep-lock
probe, then releases the lock before callbacks. This gives ready registries a no-yield
fast path and prevents a concurrent forced reload from exposing an empty/partial
callback list between the readiness probe and dispatch. A ready registry can still
be locked by deferred platform loading, activation or single-plugin unload; that
contention waits through the existing bounded foreground-lock barrier off-loop,
then re-probes readiness. It must not spin through worker hops just because
`_delivery_manager()` can already return the ready manager. Timeout or lock-wait
failure refuses policy delivery with `PluginDiscoveryIncomplete`. Cold startup and forced
reload await discovery off-loop with `asyncio.to_thread`, then await
callbacks on the caller's event loop. `pre_gateway_dispatch` is an ingress policy
gate, not a fail-open observer: the first message waits for its skip/rewrite policy,
and discovery timeout raises `PluginDiscoveryIncomplete` rather than authorizing
uninspected text. Callback exception handling is unchanged. Gateway post-admission and platform-event probes
also run off-loop; platform-event sync delivery runs there too. Synchronous delivery
called directly on a running loop refuses unready discovery without joining or acquiring
the lock. The `pre_tool_call` directive path turns that refusal into a block, never an
empty policy decision. No hook is delivered against a half-loaded registry.

Plain `discover_plugins()` listing/completion callers log a warning and return the
available registry on a background join timeout, without starting a second sweep. The
strict timeout exception remains in hook delivery, not in skill command completion.
Gateway startup explicitly requests `require_complete=True` off-loop and lets a
not-ready error abort startup before relay registration or adapter connection.
Bare-runner dispatch harnesses complete actual discovery during setup to mirror
this startup prerequisite; their short settle/spin windows do not stand in for
plugin readiness.

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
