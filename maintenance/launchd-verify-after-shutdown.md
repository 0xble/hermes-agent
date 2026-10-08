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
