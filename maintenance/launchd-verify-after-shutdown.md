# Launchd restart verification after shutdown

## Contract and placement

`hermes update` verifies a launchd gateway restart by waiting for launchd to
supervise a PID different from the pre-restart one. The respawn window
(`LAUNCHD_SUPERVISION_VERIFY_TIMEOUT`) starts only once launchd stops reporting
the old PID. Until then, the old gateway is still in its graceful restart drain.
That drain is bounded separately by the gateway's own restart exit budget plus
launchd's `ExitTimeOut` clamp. A restart that never happens, where the old PID
never exits, still fails. So does a replacement that never appears, where the
old PID is gone but no new PID arrives. Both stay bounded.
This is an updater verification invariant with no plugin or config surface, so
a narrow core patch in `hermes_cli/gateway_launchd.py` is the smallest adequate
repair.

Fork patch identity: `launchd-verify-after-shutdown`.

## Evidence and provenance

On 2026-09-23, promoting fork `0c981bd` reported `✗ ai.hermes.gateway restarted
but launchd is not supervising a new process for it` and exited 1 with a
`partial` receipt. The code swap had succeeded. The old gateway (PID 61224)
took 32.7s to drain 4 agents, 6 tool subprocesses, 3 delegations and a cron job.
launchd then started the replacement (PID 740) at once. The 20s window had
been measured from the restart request, so the drain alone exhausted it.

The verifier is identical on upstream `main`; this is not fork-induced. Upstream
[PR #94768](https://github.com/NousResearch/hermes-agent/pull/94768) raises the
constant to 45s. That still counts the drain, and a busier gateway can exceed it.
Our field timeline and this design were posted on that PR as
[a comment](https://github.com/NousResearch/hermes-agent/pull/94768#issuecomment-5808286882).

Own upstream [#121117](https://github.com/NousResearch/hermes-agent/pull/121117)
contributes the separate shutdown and respawn windows. The 2026-09-26 audit
updated its host PID/ancestry test isolation and validation, with 40 tests passing
across the verifier and fleet-restart files. The PR remains open.

## Plist reload path

Fork patch identity: `launchd-reload-exit-budget`.

When the update also rewrites the plist, the restart runs through the deferred
reload helper instead of SIGUSR1. On 2026-10-06 three updates in a row
(06:22, 10:15, 10:39 PDT) reported the same `✗ ai.hermes.gateway restarted but
launchd is not supervising a new process` with a `partial` receipt, although
each new release was serving. The helper booted out the old gateway, waited only
the 30s reload budget for it to exit, and bootstrapped while teardown was still
running (38s: interrupt agents, kill 17 tool subprocesses, disconnect adapters).
The early replacement exited with "A gateway already owns this host". launchd
then held the next relaunch for `ThrottleInterval` (30s), which landed past the
20s respawn window.

The helper now waits for the old PID until launchd must have SIGKILLed it
(`ExitTimeOut` clamp plus 5s, or the reload budget when larger), before the
first bootstrap. The respawn window is derived from the generated
`ThrottleInterval`, so a replacement that still exits early and is relaunched
one throttle later passes instead of failing. Regression:
`tests/hermes_cli/test_launchd_reload_exit_budget.py`.

The helper bootstraps immediately once the old PID is gone and `launchctl print`
confirms its label is unloaded. A still-loaded label is polled within the existing
reload budget, rather than paying an unconditional post-exit second. Every non-zero
bootstrap failure retries until that shared budget's deadline, not only EIO/EALREADY
(5/37), so a short-lived unknown launchd error cannot leave the gateway unregistered.
The backoff starts at 0.2s, doubles to a 2s ceiling, and is clipped to the
remaining budget; the clipped sleep is followed by one final bootstrap attempt.
A successful bootstrap waits for a positive supervised PID without registering
again. Failure logs distinguish a label that never unloaded, bootstrap failures
that exhausted the budget, and a bootstrapped job with no positive PID. Each
includes the last bootstrap return code, or `not-attempted` when the label never
unloaded. The `launchctl print` probe is best-effort on macOS-26 per-user domains
and falls back to the same retry path. An old label's PID cannot suppress its
unload failure. The old-PID exit ceiling and initial helper handoff delay are
unchanged. Regression: `tests/hermes_cli/test_launchd_reload_handoff.py` executes
the generated shell against fake launchctl for immediate success, unknown and
sustained bootstrap errors that outlast the previous attempt cap and then recover,
retry-budget exhaustion including the final attempt, delayed or missing PID, and
delayed or never-completed label unload. No host service is touched.

## Planned restart on reload bootout

Fork patch identity: `launchd-reload-planned-restart`.

The reload's `launchctl bootout` delivers a plain SIGTERM, which the gateway
read as an unplanned signal: it exited 1 after the unbounded post-interrupt tool
sweep. In the field that sweep took 36.80s (13 subprocesses) and 53.62s (25), so
Telegram was back only 60-77s after the signal. Every gateway-label bootout that
only reloads the definition now first writes `.gateway-planned-restart.json`
naming the gateway PID (`gateway.status.write_planned_restart_marker`): the
deferred helper (written in Python before `launchctl submit`), its in-process
fallback, the `launchd_restart` unloaded branch, the guardian rollback
`reload_target` (into the gateway's home), and the stale-label (EIO) recovery
bootout in `_launchctl_bootstrap` when the label is the gateway's (reached by
`install --force` over a live service; the PID comes from `launchctl list`
within the call's shared timeout, so a failed lookup keeps the plain bootout).
The shutdown handler consumes it one-shot and calls
`runner.stop(restart=True, service_restart=True)`, the stop a SIGUSR1 restart
reaches, without the after-turn wait that `ExitTimeOut` cannot cover. The sweep is then bounded at 2s and the exit is 75. An unknown PID writes
no marker and keeps the old behavior. Stops, takeovers and SIGINT take
precedence. No drain or shutdown timeout changed; the restart path's existing
5s post-interrupt agent grace replaces the 1s signal grace for this SIGTERM.
Regressions: `tests/gateway/test_planned_restart_signal.py`,
`test_launchd_reload_exit_budget.py::test_deferred_reload_marks_planned_restart_before_bootout`,
`test_launchd_reload_exit_budget.py::test_bootstrap_eio_recovery_marks_planned_restart_before_bootout`,
`test_gateway_guardian.py::test_rollback_marks_planned_restart_before_bootout`.
Retire with the bootout path itself, or when upstream distinguishes a planned
launchd reload from an external kill.

## Verification and retirement

Run scripts/run_tests.sh for
tests/hermes_cli/test_launchd_reload_exit_budget.py,
tests/hermes_cli/test_update_launchd_restart_verification.py,
tests/hermes_cli/test_update_launchd_fleet_restart.py, and
tests/hermes_cli/test_gateway_service.py. The regressions replay the field
timeline: 33s of the old PID draining, then a replacement, which must pass. They
also check that a job which never returns still fails within the respawn window,
and that an old PID which never exits still fails within the shutdown budget.

Retire when the selected upstream release verifies a slow-draining restart
without the local change, whether through PR #94768 revised as suggested or an
equivalent. Roll back by reverting the change.
