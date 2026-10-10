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
  Retain descendant witnesses through release, including after the group leader
  exits; expand a group only while a previously recorded witness is verified.
- Numeric PID/PGID ledgers alone never authorize a signal. Dead, recycled or
  unreadable incarnations return without waiting. Missing witnesses fail closed;
  a descendant never captured before every known witness exits cannot safely be
  adopted just from its numeric group and may remain unreaped.
- Give all owned survivors one shared two-second SIGTERM grace using
  `psutil.wait_procs`, then revalidate before escalation. Never kill the gateway's
  own group. Windows tree signalling receives the original verified parent handle.
- Keep per-server scoping and release supervisor ownership on completed sweeps.
  Do not restore an unconditional sleep merely because the ledger is nonempty.

When upstream changes tracking or teardown, inspect capture/release/reap as a
single unit. Retire this patch only when upstream has equivalent incarnation-safe,
completion-driven grace and the local regressions pass unchanged.

## Proof

`tests/tools/test_mcp_reaper_grace.py` covers dead/empty/unreadable/reused ledgers,
cooperative and SIGTERM-ignoring throwaway children, and surviving grandchildren
with or without an exited group leader. Synchronization uses child readiness
pipes; fake signal tests never touch live host processes. Existing stability and
parent-death supervisor tests now seed incarnation evidence, not numeric PID-only
signal authority.

Run the MCP lifecycle/shutdown and gateway shutdown files through
`scripts/run_tests.sh`, lint touched source/tests, and require
`python scripts/check_fork_patches.py --repo . --source-only` to return zero problems.
