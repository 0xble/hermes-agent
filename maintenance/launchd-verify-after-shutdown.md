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

## Verification and retirement

Run scripts/run_tests.sh for
tests/hermes_cli/test_update_launchd_restart_verification.py,
tests/hermes_cli/test_update_launchd_fleet_restart.py, and
tests/hermes_cli/test_gateway_service.py. The regressions replay the field
timeline: 33s of the old PID draining, then a replacement, which must pass. They
also check that a job which never returns still fails within the respawn window,
and that an old PID which never exits still fails within the shutdown budget.

Retire when the selected upstream release verifies a slow-draining restart
without the local change, whether through PR #94768 revised as suggested or an
equivalent. Roll back by reverting the change.
