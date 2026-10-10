# MCP reaper live-process grace

## Provenance

Fork patch identity: `mcp-reaper-live-grace`.

The reaper and stdio child ledger were upstream code: history for
`tools/mcp_tool_lifecycle.py`, `tools/mcp_tool_transport.py` and the pre-split
`tools/mcp_tool.py` contains no owning `Fork-Patch` trailer for this path.
This new divergence removes a blind two-second shutdown/reconnect delay without
reducing the grace for children that still own their spawn incarnation.

## Invariants and update rule

- Cache psutil PID/create-time handles while stdio children are being tracked.
  Retain descendant witnesses through release. The release path may use the
  recorded-session check once to discover a late-spawned member; later refreshes
  and reaping require a previously recorded live, create-time-verified witness.
- Numeric PID/PGID ledgers alone never authorize a signal. A late-spawned member
  is adopted only when the release-time session check records it or a live witness
  verifies the group; a recycled PGID with an unrelated session is rejected.
  Missing or unreadable witnesses fail closed.
- Give all owned survivors one shared two-second SIGTERM grace using
  `psutil.wait_procs`, then revalidate before escalation. Never kill the gateway's
  own group. Windows tree signalling receives the original verified parent handle.
- Keep per-server scoping. Retain parent-death supervisor registration for groups
  whose liveness or identity cannot be verified; unregister only groups verified
  empty after the graceful sweep. Do not restore an unconditional sleep merely
  because the ledger is nonempty.

When upstream changes tracking or teardown, inspect capture/release/reap as a
single unit. Retire this patch only when upstream has equivalent incarnation-safe,
completion-driven grace and the local regressions pass unchanged.

## Proof

`tests/tools/test_mcp_reaper_grace.py` covers dead/empty/unreadable/reused ledgers,
cooperative and SIGTERM-ignoring throwaway children, surviving grandchildren with
or without an exited group leader, and rejection of a recycled PGID in an unrelated
session. Synchronization uses child readiness pipes; fake signal tests never touch
live host processes. Existing stability and parent-death supervisor tests now seed
incarnation evidence, not numeric PID-only signal authority.

Run the MCP lifecycle/shutdown and gateway shutdown files through
`scripts/run_tests.sh`, lint touched source/tests, and require
`python scripts/check_fork_patches.py --repo . --source-only` to return zero problems.
