# Honest compression notices

Load this unit when changing the deferred-command queue in `gateway/platforms/base.py`, adapter teardown (`cancel_background_tasks`), or the `gateway.busy.deferred_dropped` locale key.

## Required behavior

A command acknowledged with "⏳ `/x` scheduled after the current turn commits" must end with exactly one visible outcome. That outcome is the command's own reply, its turn-error notice, or `gateway.busy.deferred_dropped` when adapter teardown (`cancel_background_tasks`: restart, shutdown, or adapter replacement) discards it. Teardown discards a command whether it is still queued or its replay is mid-flight. The deferred queue stays in memory, so it is not persisted or replayed across a restart. A user's own `/stop`, `/new` or `/reset` drops queued commands silently, as before. Reporting is bounded at 5s and never blocks teardown.

The hygiene turn-hold notices are owned by [Hygiene deferred adoption](hygiene-deferred-adoption.md), which already keeps the false "continuing without compression" notice off the keep-admission branch.

## Provenance and patches

Fork patch identity: `honest-compression-notices`.

Fork-original. This change followed the 2026-10-13 Agent Evals topic incident, where a deferred `/compact` was lost to a gateway restart without any reply.

## Verification

Contract tests: `tests/gateway/test_cancel_background_drain.py`

`scripts/run_tests.sh tests/gateway/test_cancel_background_drain.py tests/gateway/test_session_race_guard.py tests/agent/test_i18n.py`

## Retirement and rollback

Retire if upstream reports deferred commands discarded at teardown. To roll back, revert the patch commit and its regressions. No persistent-data migration is required.
