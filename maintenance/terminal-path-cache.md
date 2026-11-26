# Terminal PATH Cache

Load this when changing `pm.install.cached_store_path_dirs`, `pm.install._store_path_dirs`, `pm.user_facing.user_facing_path`, how `tools/environments/local.py` builds a run environment, or `pm.filesystem.durable_write_bytes`.

## Required Behavior and Provenance

Fork patch identity: `terminal-path-cache`.

Every local terminal command builds its PATH through `user_facing_path`, which reads the store's tool dirs. Each fresh composition resolves the store root (realpath plus an install-stamp walk), reads every root's facts, and probes every installed package's binary. Chromium's probe is an `rglob` of its whole install tree. On 2026-10-25 the Personal gateway ran 120 active agents at 216-277% CPU across 640 threads. A 30 s `py-spy --gil --nonblocking` sample of pid 16781 attributed 23% of all GIL-held samples to this composition, so it ran once for every bash tool call. That GIL pressure also stretched SQLite writer-lock holds: every row step releases the GIL and then waits behind CPU-bound threads to take it back. See [gateway loop state.db guard](gateway-loop-statedb-guard.md).

`cached_store_path_dirs` caches the composition per process. Its key covers:

- `pm.filesystem.write_generation`, which every PM durable write bumps (facts, stamps, selections).
- The `st_mtime_ns` and size of every root's `facts.json`. An install, uninstall or repair by another process (an agent running `hermes pm install X` in its terminal) therefore shows up on the very next command. Without this, the new tool would be missing for up to the TTL.
- A 30 s TTL bucket. It bounds out-of-band damage, such as a deleted binary, that leaves the facts untouched.
- `HERMES_RUNTIME_DIR`, `HERMES_HOME`, `HOME` and the active install store, which are the environment the store roots resolve from. The resolved roots are cached under that same key, so a cache hit costs only a few `stat` calls. The bound profile home is deliberately left out of the key. Composition never reads it, and keying on it would make a gateway that serves several profiles miss on every interleaved command.

A lock serializes recomposition, so commands that all miss at a TTL rollover compose only once.

Callers get a copy, never the cached list. Startup activation (`activate`, `store_first_path`) stays uncached, because it must report live damage (`test_drift_activates_intact_chains_and_reports_the_live_verdict`). A stale entry can only misorder or omit a store dir in a terminal child's PATH for up to the TTL. Correctness checks, installs and activation never read the cache.

## Verification

`tests/pm/test_store_path_dirs_cache.py`: on main, 6 terminal compositions take 6 full composes. Here they take 1. A PM write, a new runtime dir, or TTL expiry recomposes, and activation stays live. The root `tests/conftest.py` clears the cache per test.
