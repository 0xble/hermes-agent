# Install and Scratch Garbage Collection

Patch identity: `install-scratch-gc`.

Hermes records the resolved checkout beside each dependency install and collects an install only after the checkout is gone, the install has been idle for a day, its non-blocking install lock is available, and no dependency-generation lease is held. Legacy installs without the sidecar are judged by tree activity and collected after three idle days when their key matches no visible checkout. Each removal first renames the install under its lock to a `.gc-` name outside the key space, so an interrupted delete leaves no remnant that looks live or legacy; the next pass finishes it. The gateway housekeeping loop runs the collection on its first tick and hourly after, on its own thread, at most ten installs per pass and never two passes at once. Release publishes also run it, but agents create and delete a worktree per task, each with a full environment, so without the hourly pass orphans accumulate by the hundred between publishes. Upstream's `pm/install_states.py` reclaim stays as is; it covers only checkouts directly under a `.worktrees/` directory or the data root, and runs only beside the worktree pruner.

Scratch maintenance must still run when macOS exports `TMPDIR`; honoring that user/OS temp directory must not bypass Hermes' scratch-tree pruner.

Guard tests: `tests/pm/test_install_gc.py` covers orphan, live, leased, locked, and legacy installs, the per-pass limit, and finishing an interrupted removal. `tests/gateway/test_install_orphan_housekeeping.py` covers the housekeeping chore collecting an orphan idle past a day while keeping a younger orphan and a live checkout's install. `tests/test_scratch_dir.py::test_user_tmpdir_does_not_disable_scratch_pruning` covers the macOS boot path.

Retire when equivalent upstream behavior is present and the guard tests pass without this fork patch.
