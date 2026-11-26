# Historical upgrade compatibility

Patch identity: `nightly-upgrade-e2e`.

The release-to-fork updater begins its takeover on the historical interpreter,
not the new pinned Python. PM archive provisioning must retain containment on
unpatched Python 3.10/3.11 stdlibs without PEP 706. The shared extractor keeps
its modern data filter and validates legacy members lazily, after preceding
links have been placed; never substitute unfiltered extraction to make an
upgrade pass. The dependency remains until the supported upgrade floor no
longer includes those interpreters.

A pre-handoff updater also leaves root modules cached while purging packages.
Backup is a first post-purge import and its state/agent graph reaches new
`utils.file_signature` before the existing config bridge. Its import must run
the same stale-root bridge before importing that graph. The historical
v2026.9.14 graph-swap test, plus a fresh-process backup-first regression,
qualifies this seam independently of import order in the unit suite.

Source surfaces: `pm/store.py`, `hermes_cli/backup.py`.
Guard surfaces: `tests/pm/test_legacy_tar_bootstrap.py`,
`tests/pm/test_archive_safety.py`,
`tests/hermes_cli/test_stale_root_module_bridge.py`, and
`tests/e2e/core/upgrade/test_fresh_process_entrypoints.py`.

Contribution decision: narrow fork repair for the nightly upgrade qualifier;
no upstream publication is authorized here. Upstream disposition: not adopted
in the accepted baseline. Fork delivery: draft PR, not runtime activation.
Retire each bridge only when equivalent upstream protection or a deliberately
raised historical upgrade floor makes its guard redundant. Read
[install and scratch GC](install-and-scratch-gc.md) when changing takeover PM
runtime lock/owner identity; the PM dependency project is not the checkout
whose dependency state recovery and collection fence.
