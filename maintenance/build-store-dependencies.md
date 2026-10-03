# Build Store Dependencies

Patch identity: `build-store-dependencies`.

Package hooks resolve dependencies from the active install store before the ambient read and writable stores. The private context resets on normal return, failure, and nested installation. Explicit lookup roots remain authoritative. Ambient fallback preserves ordinary installs whose dependencies already live in a sealed read store.

Reproduction: `prepare_tools(["node", "npm"], out=build_store, target=target)` installs Node into its caller-owned store, but npm previously searched only ambient stores during unpack. A cold runtime store failed with `npm extends node, which is not installed`. A warm ambient store could supply the wrong dependency instance. ARM64 Windows ripgrep's Python DLL staging uses the same dependency lookup and receives the same fix.

Proof surfaces: `tests/pm/test_build_dependency_lookup.py` exercises real downloads, lockfiles, facts, archive unpack, and publication for cold and warm stores, both hook phases, explicit lookup roots, and nested failure cleanup. `tests/cron/test_immutable_worker_real.py` exercises real isolated release staging and detached workers. The PM tree at frozen upstream main `484ebdf16a5127894f16ba883e9630968f945c5e` was byte-identical to the failing candidate before this patch.

Retire when upstream binds package dependency lookup to the active installation store and these invariants pass without this implementation. Rollback removes the focused PM context change and its invariant tests, preserving the separate archived web-build preparation patch.
