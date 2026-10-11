# Bounded macOS Sidecar Holder Scan

**Patch identity:** `state-db-bounded-holder-scan`.

The native deleted-WAL/SHM guard can encounter an unrelated vnode whose libproc
metadata read blocks in the kernel. Concurrent database checks share one daemon
enumeration with the existing five-second budget. A timeout starts a thirty-second
cooldown. A still-blocked worker is reused after cooldown, so repeated SessionDB
opens cannot accumulate abandoned scans. Failure warnings include the PID,
descriptor and syscall stage, without reading process arguments or file contents.

Each check separately inspects this process's own sidecars using `F_GETPATH`,
then checks descriptor identity only for matching sidecars. This avoids libproc's
stat of unrelated open vnodes and keeps the lost-generation check effective during
foreign-scan cooldown. Matching descriptor paths and identities are checked twice
to reject reuse races. No SQLite descriptor is opened or closed by that check.
Foreign scans retain the existing fail-open timeout policy. Completed scans are
judged against each sidecar path immediately after reading its descriptor, then
filtered by each caller's watched paths. This avoids interpreting a healthy
connection's later close as a lost generation. No completed result is cached.
Non-vnode descriptors are skipped before libproc reads.

Proof: `scripts/run_tests.sh tests/hermes_state/test_darwin_scan_bounds.py tests/hermes_state/test_state_db_darwin_fd_scan_hang.py tests/hermes_state/test_deleted_wal_generation_guard.py`.
The tests cover one worker under concurrent blocked probes, recovery, a newly
retired self-held sidecar during cooldown, native descriptor identity, and WAL
preservation through normal process exit.

This extends upstream's `233434b41447` fail-open timeout implementation. Retire
when a selected released upstream version preserves bounded worker count and
in-process detection during a blocked foreign scan. Revert the commit carrying
`Fork-Patch: state-db-bounded-holder-scan` to restore per-open enumeration.
