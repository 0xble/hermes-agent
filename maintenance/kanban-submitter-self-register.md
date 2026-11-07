# Kanban Submitter Self-Registration

Fork patch identity: `kanban-submitter-self-register`.

## Required Behavior

The owner submitter calls `kanban_db_dispatch.adopt_worker_pid` directly before
clearing the pinned board/task/run environment and before connecting to the
owner. It uses `kanban_db_connect.connect` on the pinned
`HERMES_KANBAN_DB`, the same connection implementation used by the CLI wrapper's
`_board(None)` path (where the environment-pinned DB wins board selection).
`contextlib.closing` closes the connection without suppressing errors.
Only successful adoption permits admission. False or any registration exception
exits nonzero before owner connection. The existing CLI wrapper remains lenient
for its existing caller, and is deliberately not the admission gate.

A dispatcher killed after spawn but before recording the PID must not leave
admitted work invisible to TTL recovery. The submitter remains the dispatcher's
process handle and retains its existing lifetime coupling to owner execution.

Registration and reclaim serialize their writes through board write
transactions. Adoption verifies running status and the current run ID, and fills
both PID/fingerprint rows when the same-host claim has no PID. If adoption wins
after reclaim's stale SELECT, reclaim's `worker_pid IS ?` CAS loses against its
NULL snapshot. The next sweep sees the live registered PID and extends the TTL
under the existing heartbeat policy. If reclaim commits first, adoption returns
False, including after a replacement run is claimed. No owner-recovery changes,
TTL increases, or diagnostic changes belong to this patch.

## Provenance And Delivery

Related upstream report: https://github.com/NousResearch/hermes-agent/issues/121556.
The CLI fix in upstream commit `63e44332f5d2ed3de409d513824c385aa8075235`
does not cover `hermes_cli.kanban_worker_client._submit` on fork base
`d26ea98988f516e2c46d455ea428e8d486ee02af`. Upstream main
`a9ff9799e273f525b82718cdc69fdec71b1dca5a` also omits that registration.
This is a local fork correction. Publication and runtime activation are outside
this delivery's authorization.

## Verification And Retirement

Contract tests: `tests/hermes_cli/test_kanban_submitter_registration.py`.

The deterministic integration tests exit the dispatcher through its spawn hook
before parent PID recording, expire the lease, and require no reclaim and
exactly 1 loopback provider request. A fake owner reads both PID/fingerprint rows
through a fresh connection at admission. Separate refusal and exception cases
require nonzero exit and no owner connection. Both race orderings use the real
submitter entrypoint and adoption contract, with adoption interleaved between
reclaim's stale SELECT and its CAS, or reclaim committed before adoption.

The test commit precedes the fix and is run against the unchanged base
implementation to establish RED. The unchanged Linux dispatcher-restart test
provides the real subprocess/SIGKILL proof beyond the deterministic spawn-hook
simulation. Exact results, commit identities and any qualification limits are
recorded in the requested scratch `result.txt`.

Run the unchanged dispatcher-restart E2E 30 times on native Linux, then the
Kanban CLI, gateway, tool and E2E suites, code health, and the committed-SHA gate.
Retire when released upstream covers owner submitters with the same behavioral
contract. Roll back by reverting the commit carrying this patch identity.
