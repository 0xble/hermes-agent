# Honest compression notices

Load this unit when changing the hygiene turn-hold notices in `gateway/run_turn.py` (`_hmwa_hygiene_on_turn_hold`), the deferred-command queue in `gateway/platforms/base.py`, or the `gateway.compress.turnhold_*` and `gateway.busy.deferred_dropped` locale keys.

## Required behavior

Hygiene turn-hold notices say only what is true.

- The cancel branch (worker not watermark-fenced) really continues this turn uncompressed, so it still sends `gateway.compress.turnhold_deferred` right away.
- The keep-admission branch (#97963) sends nothing when the turn is released. Its summary is usually adopted moments later, so success stays silent. Only if the detached attempt ends without committing does the done-callback send `gateway.compress.turnhold_failed` once. It sends nothing while the gateway is draining, because the restart notice already covers that case.

A command acknowledged with "⏳ `/x` scheduled after the current turn commits" must end with exactly one visible outcome. That outcome is the command's own reply, its turn-error notice, or `gateway.busy.deferred_dropped` when adapter teardown (`cancel_background_tasks`: restart, shutdown, or adapter replacement) discards it. Teardown discards a command whether it is still queued or its replay is mid-flight. The deferred queue stays in memory, so it is not persisted or replayed across a restart. A user's own `/stop`, `/new` or `/reset` drops queued commands silently, as before. Reporting is bounded at 5s and never blocks teardown.

## Provenance and patches

Fork patch identity: `honest-compression-notices`.

Fork-original. This change followed the 2026-10-13 Agent Evals topic incident. Two "compression deferred" notices were sent although both summaries were adopted minutes later. A deferred `/compact` was also lost to a gateway restart without any reply.

## Verification

`scripts/run_tests.sh tests/gateway/test_session_hygiene_turnhold_adoption.py tests/gateway/test_cancel_background_drain.py tests/gateway/test_session_race_guard.py tests/agent/test_i18n.py`

## Retirement and rollback

Retire if upstream makes turn-hold notices outcome-based and reports deferred commands discarded at teardown. To roll back, revert the patch commit and its regressions. No persistent-data migration is required.
