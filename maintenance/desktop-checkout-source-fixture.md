# Desktop checkout-source hand-off fixture

Load when changing `apps/desktop/electron/updater/checkout-source.test.ts`, the
legacy (pre-protocol-2) hand-off correlation in `waitForHandoffClaim` /
`handoffTakenBy`, or how the Desktop passes `HERMES_UPDATE_STARTED_AT` to the
hand-off script.

Fork patch identity: `desktop-checkout-source-fixture`.

## Required behavior

The test `carries each install channel from Python publication checks into the
source handoff` stands in for the hand-off script by writing the update marker
from its `spawnUpdaterProcess` mock. That marker must echo the
`HERMES_UPDATE_STARTED_AT` value the Desktop passed in the spawn environment,
exactly as `scripts/desktop-update/posix.sh` does (`STARTED_AT=
"${HERMES_UPDATE_STARTED_AT:-$NOW}"`) and as `checkout-handoff.test.ts`'s
`scriptTakesMarker` already does.

## Root cause

The fixture script is empty, so `readHandoffProtocol` returns 1 and the Desktop
correlates the claim the legacy way: line 2 of the marker must equal the
`updateStartedAt` second it computed before spawning
(`handoffTakenBy`: `marker.startedAt === startedAt`). The mock instead wrote
`Math.floor(Date.now() / 1000)`, a second read of the wall clock. Between the
Desktop's read (`applyPosixHandoff`) and the mock's read the strategy awaits
`updateHandoffConflict`, `planHandoff`, and argument assembly. When a
wall-clock second boundary falls in that gap — far more likely when the host is
loaded — the two values differ, `waitForHandoffClaim` never sees a correlated
claim, spins until `handoffClaimTimeoutMs` (2 s in the test), and `apply()`
returns `{ ok: false, error: 'updater-spawn-failed', message: 'The updater did
not start … never took the update lock within 2 s.' }`.

Production is not affected: the real script echoes the env value, so its
legacy claim always correlates. The defect was test-only.

## Evidence

- Reproduced in a local ci-gate run on 2026-10-13 (load ≈ 20–25):
  `checkout-source.test.ts:462` received `{ ok: false, …(3) }` after 9283 ms.
- RED/GREEN under load (4 concurrent copies per round plus 20 busy-loop CPU
  hogs on 28 cores, inside one heavy-slot): see the PR description for counts
  and the full RED failure object.

## Retirement

Retire when upstream's copy of this test echoes the spawn environment's
`HERMES_UPDATE_STARTED_AT` (or the fixture moves to protocol 2 run-id
correlation).
