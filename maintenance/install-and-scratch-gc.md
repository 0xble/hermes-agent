# Install and Scratch Garbage Collection

Patch identity: `install-scratch-gc`.

Hermes records the resolved checkout beside each dependency install and collects an install only after the checkout is gone, the install has been idle through a grace period, its non-blocking install lock is available, and no dependency-generation lease is held. Legacy installs without the sidecar are judged by tree activity and collected after three idle days when their key matches no visible checkout. Scratch maintenance must still run when macOS exports `TMPDIR`; honoring that user/OS temp directory must not bypass Hermes' scratch-tree pruner.

Guard tests: `tests/pm/test_install_gc.py` covers orphan, live, leased, locked, and legacy installs. `tests/test_scratch_dir.py::test_user_tmpdir_does_not_disable_scratch_pruning` covers the macOS boot path.

Retire when equivalent upstream behavior is present and the guard tests pass without this fork patch.
