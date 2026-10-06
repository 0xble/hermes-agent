# Oneshot plugin-hook discovery

Fork patch identity: `oneshot-plugin-hook-discovery-barrier`.

## Contract

A CLI one-shot turn must not invoke lifecycle hooks against a partially loaded
plugin registry. Startup may launch plugin discovery in a daemon thread, and
`PluginManager.discover_and_load()` marks its registry discovered before loading
plugin modules to prevent recursive discovery. Hook delivery must therefore
wait for an in-flight discovery worker before reading the registry.

## Failure and fix

The oneshot entrypoint starts background plugin discovery and then builds the
first turn. The delivery path treated `_discovered=True` as equivalent to
"discovery complete" and skipped `_join_background_discovery()`. Under startup
contention, `pre_llm_call` observed an empty hook registry, so the turn still
completed but the plugin marker and injected canary were absent.

`_delivery_manager()` now joins the background worker before reading the
registry. The barrier is bounded without reopening the partial-registry race:
when `plugins.load_timeout_seconds` is positive, its deadline is the effective
per-plugin timeout plus five seconds of slack, with a 30-second minimum for
multi-plugin scans; when the per-plugin deadline is disabled with `0`, the
outer barrier uses a separate fixed 30-second cap. These are hard outer limits,
so a filesystem scan or plugin import that does not return cannot hang every
delivery path forever.

If the outer deadline expires, module-level discovery state becomes explicitly
incomplete. One ERROR records the current plugin when the loader knows it (or
says that the scan stage is unknown), and hook/middleware/prompt delivery fails
closed with a warning: no partial plugin callbacks are invoked, `has_hook()`
and callback snapshots report no plugin hooks, and later callers do not wait
through the same deadline again. A worker that later completes a full sweep
clears the state and hook delivery resumes; a failed sweep remains closed.

The current plugin field is set around each manifest's dependency check,
configuration validation, and load/register operation, while filesystem scans
remain reported as unknown. The per-plugin loader deadline and abandoned-loader
cap remain unchanged.

## Verification and retirement

Verify `tests/hermes_cli/test_plugin_delivery_discovery.py` and the plugin
load-timeout tests. The parity one-shot probe must report both the plugin marker
and injected canary under constrained Linux startup. The regression suite covers
waiting past the former 30-second join cap, the disabled-deadline hard bound and
fail-closed behavior without repeated waits, and late completion reopening hook
delivery.

Retire this patch when the fork's upstream baseline guarantees the same bounded
discovery barrier and explicit fail-closed completion state.
