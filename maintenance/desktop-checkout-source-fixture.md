# Desktop Source Handoff Fixture

Fork patch identity: `desktop-checkout-source-fixture`.

## Required Behavior

The spawn mock in `apps/desktop/electron/updater/checkout-source.test.ts`
must write the supplied `HERMES_UPDATE_STARTED_AT` into its legacy claim.
The production `posix.sh` and the sibling `checkout-handoff.test.ts` fixture
already use that value. Sampling the clock again can cross a second boundary,
so the mock's claim never matches the Desktop's exact timestamp correlation.

This changes only the test fixture. Production marker ownership, handoff
protocols, deadlines and source-resolution assertions remain unchanged.

## Source And Verification

The correction adopts the timestamp fix from fork PR
[#593](https://github.com/0xble/hermes-agent/pull/593), commit
`fae95b49fe0a64dc35b64a4a1e1474758e33725c`. That draft remains a separate
historical candidate, not evidence of integration or a passing gate.

A controlled reproduction on the received-handover candidate passed the
unmodified fixture and normal diagnostic. Delaying the mock by 1100 ms failed
with `updater-spawn-failed`. The same delay passed when the mock echoed the
supplied timestamp. This proves the fixture defect, not the cause of every
historical failure. The reproduction logs are retained with that PR's evidence.

Related upstream PRs #132345 and #136491 establish the legacy claim and
source-resolution contracts. Neither removes the mock's second clock read.
No upstream contribution or live activation is included in this patch.

## Retirement And Rollback

Retire when upstream supplies equivalent fixture correlation or replaces this
legacy fixture with another protocol. Revert commits carrying
`Fork-Patch: desktop-checkout-source-fixture` to roll back.
