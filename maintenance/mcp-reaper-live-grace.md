# MCP reaper live-process grace

## Provenance

Fork patch identity: `mcp-reaper-live-grace`.

The reaper and stdio child ledger were upstream code: history for
`tools/mcp_tool_lifecycle.py`, `tools/mcp_tool_transport.py` and the pre-split
`tools/mcp_tool.py` contains no owning `Fork-Patch` trailer for this path.
This new divergence removes a blind two-second shutdown/reconnect delay without
reducing the grace for children that still own their spawn incarnation.

## Invariants and update rule

- Cache psutil PID/create-time handles at stdio spawn. Refresh descendant
  witnesses in `_run_stdio`'s inner `finally`, while still inside `stdio_client`,
  **before** SDK teardown closes stdin and waits for the wrapper to exit. This
  capture runs on normal shutdown, session/handshake errors and cancellation.
  Refresh extends only existing live, create-time-verified handles, revalidated
  after descendant enumeration; it never recaptures a numeric wrapper PID or
  replaces earlier descendant evidence when a wrapper has already exited.
  Retain these witnesses through post-SDK release. Both release and reaping may
  discover new group members only through a previously recorded, live,
  create-time-verified witness in that group, revalidated after enumeration.
- Numeric PID/PGID/SID equality never authorizes adoption or a signal. This
  remains true if a replacement setsid leader has already exited, leaving children
  with the original numeric SID/PGID, or if spawn capture produced no witnesses.
  Missing, dead or unreadable witnesses fail closed even for an include-active
  shutdown sweep; there is no release-only or empty-ledger exception.
- Preserve upstream v0.21.6's leader fingerprint capture and drift-tolerant
  PID-reuse rejection as an additional veto before each signal phase. An absent,
  unreadable or matching fingerprint never grants ownership: live recorded
  create-time witnesses remain mandatory. A reaped leader does not prevent
  SIGKILL escalation for its verified SIGTERM-ignoring descendants; use their
  incarnation handles rather than a numeric `killpg(pgid, 0)` adoption path.
- Give all owned survivors one shared two-second SIGTERM grace using
  `psutil.wait_procs`, then revalidate before escalation. Never kill the gateway's
  own group. Windows tree signalling receives the original verified parent handle.
- Keep per-server scoping. Retain parent-death supervisor registration for live
  groups lacking ownership evidence or whose liveness/identity cannot be read;
  unregister only groups verified empty or positively identified as foreign.
  Do not restore an unconditional sleep merely because the ledger is nonempty.

When upstream changes tracking or teardown, inspect capture/release/reap as a
single unit. Retire this patch only when upstream has equivalent incarnation-safe,
completion-driven grace and the local regressions pass unchanged.

## Proof

`tests/tools/test_mcp_reaper_grace.py` covers dead/empty/unreadable/reused ledgers,
cooperative and SIGTERM-ignoring throwaway children, surviving grandchildren with
or without an exited group leader, and rejection of reused groups both with an
unrelated session and with matching numeric SID/PGID after the replacement leader
exits. The empty-witness active-ledger regression exercises final shutdown.
Synchronization uses child readiness pipes; fake signal tests never touch
live host processes. `test_grandchild_reaped_via_pgroup` executes the real
`_run_stdio` capture/release path with a pipe-controlled wrapper and late helper:
track → helper spawn → pre-SDK witness capture → stdin EOF → wrapper exit →
release → reaper. It fails on `55da6807` because the helper survives, and covers
normal shutdown, errors and cancellation. Refresh regressions reject empty,
dead, recycled and unreadable witnesses, including one that exits during a scan.
Existing stability and parent-death supervisor tests seed incarnation evidence,
not numeric PID-only signal authority.

Run the MCP lifecycle/shutdown and gateway shutdown files through
`scripts/run_tests.sh`, lint touched source/tests, and require
`python scripts/check_fork_patches.py --repo . --source-only` to return zero problems.
