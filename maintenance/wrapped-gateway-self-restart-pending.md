# Wrapped launchd gateway self-restart pending

## Contract and placement

When `hermes update` runs inside the gateway it restarts (`request_update`, a
cron job in the gateway tree), the restart is deferred until the updater exits.
The fleet matrix marks that gateway `restart pending`, not STALE, by matching its
row PID against the set the restart phase records (#119597). Because launchd can
supervise a wrapper, the osascript TCC launcher, whose child is the real gateway,
the recorded set must also hold the gateway PID from `get_running_pid()`, the one
the matrix row carries. That PID is added only when it also encloses the updater,
so any other gateway on the old code still fails the update. This is an updater
verification invariant with no plugin or config surface, so a narrow core patch
in `hermes_cli/update_cmd_fleet.py` is the smallest adequate repair.

Fork patch identity: `wrapped-gateway-self-restart-pending`.

## Evidence and provenance

On 2026-09-24 at 23:24 and 2026-09-25 at 04:43, promotions through
`request_update` reported `✗ default (pid …) — STALE (pre-update code)` and
exited 1 with a `partial` receipt. In both cases the gateway drained, restarted
and came back on the new code: the next receipt started from it, and the live
gateway reported `6cb23cc`. launchd supervised the osascript wrapper (PID 48792),
while the gateway and its status file used the child (PID 48794). The restart
phase recorded only the wrapper, so the matrix row kept its stale verdict.
Updates before the upstream #119597 change reached the fork waited for a new
supervised PID instead, and passed. Upstream `main` records the same single PID.

## Verification and retirement

Run scripts/run_tests.sh for
tests/hermes_cli/test_fleet_matrix_self_restart_pending.py,
tests/hermes_cli/test_update_launchd_restart_verification.py,
tests/hermes_cli/test_update_cron_deadlock_guard.py, and
tests/hermes_cli/test_gateway_launchd_supervised_child.py. The regressions use
real process ancestry. A wrapper-supervised gateway enclosing the updater must
render `restart pending` and pass. A gateway PID outside the updater's tree must
never be added.

Retire when the selected upstream release records the gateway's own PID for a
wrapper-supervised self-restart without the local change. Roll back by reverting
the change.
