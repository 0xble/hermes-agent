# Guardian Rollback Restore

Load this unit before changing `gateway_guardian.rollback_switch`, `_restore_running`,
`secret_preflight`, or the `restore` operation in `immutable_releases.activate_release`.
The rollback itself, its canary trigger and its deadlines belong to the
[post-install admission canary](release-defects.md#post-install-admission-canary).

## Required Behavior

A guardian rollback never leaves the gateway down for a release it cannot start.

- Before the serving gateway is touched, `secret_preflight` refuses a rollback definition
  whose PATH cannot resolve the configured 1Password `op` CLI, or whose pinned
  `binary_path` is not executable. The serving release keeps running.
- If the rollback target is not healthy by the run deadline, or the rollback raises after
  bootout, the guardian restores the serving release only when all of these hold:
  - That release was proven to run when the rollback began: its switch carries a
    `reload_ack`, or it passed the running-release health proof. An expired,
    never-acknowledged switch is not restored.
  - The target is proven down: parked, unloaded, or no supervised PID. A target that is
    still running gets its own `STARTUP_SECONDS` window first. If it is still running and
    unhealthy after that, it is left alone. A probe that cannot answer is not proof.
- The restore re-activates the serving release with the exact plist bytes it last started
  with, as operation `restore`.
  - The admission canary does not watch it, because it is not a promote.
  - A later `hermes update --rollback` still rolls back, because it is not a completed
    rollback.
  - The unacknowledged rollback transaction is archived as `release-abandoned-*.json`,
    not completed.
  - It writes `restore` receipts: attempt, then restored, failed or skipped with a reason.
- A failure before bootout still fails closed.

## Source

Fork patch identity: `post-install-admission-canary` (slice: rollback restores the serving
release).

On 2026-10-29 the guardian acted on bff96cdf's tripped canary and rolled back to d32adcc,
even though bff96cdf was serving fine. The rollback plist could not find `op`, so the
rolled-back gateway loaded no platform tokens and exited cleanly. The guardian then
re-bootstrapped the same broken definition. The gateway was down from 11:07 PM to
11:13 PM PT.

Fork-only: upstream has no immutable-release guardian.

## Verification And Rollback

Guard: `scripts/run_tests.sh tests/hermes_cli/test_guardian_rollback_restore.py
tests/hermes_cli/test_gateway_guardian.py`. One test uses the real `rollback()` and
`activate_release`, with only the gateway acknowledgement observed through a stub.

To roll back, revert this slice. Existing `restore` transactions in `release-last-txn.json`
then read as unsupported only while one is pending. Settle or archive a pending `restore`
before downgrading.
