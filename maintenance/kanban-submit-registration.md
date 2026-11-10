# Kanban submitter registration

Fork patch identity: `kanban-submit-registration`.
Contract tests: `tests/e2e/core/kanban/test_kanban_dispatch_registration.py`.

## Required behavior

A dispatcher killed between spawning and recording a worker must not allow TTL
recovery to execute that card again beside the original attempt. The submitter
registers its PID through the existing `adopt_worker_pid` write transaction before
owner startup or admission. If reclaim wins first, registration refuses the stale
run and no user work is submitted. Unreadable registration fails closed.

## Diagnosis

The self-registration in upstream `63e44332f5d2ed3de409d513824c385aa8075235`
only runs through the former single-query CLI worker. The dispatcher now launches
`hermes_cli.kanban_worker_client`, which previously proceeded directly to owner
admission. A bound interpreter can therefore execute with no submitter PID in
`tasks` or `task_runs`. An expired-claim tick releases that claim while its
interpreter is live. The new run completes successfully and the first run's stale
completion is refused, concealing duplicate user effects behind 1 completed run.

The regression uses FIFO barriers around the actual dispatcher's claim, spawn,
and PID recording. It covers registration both before and after recording, kills
the dispatcher with SIGKILL, expires only the isolated test claim, and holds the
first provider response across a recovery tick. A real terminal command appends
to a file. Base executes that command twice in the spawn/record gap, independently
of the existing E2E's provider counter. The original restart E2E is unchanged.

## Scope and retirement

Stack fit: existing Python worker client, SQLite transaction and PID adoption,
pytest, recording fake provider and real-process board harness. No dependencies,
schema, configurable hooks, timeouts, retries, or live runtime changes.

Prior art checked: upstream PR 141187 concerns single-query registration-exit
accounting and fingerprint reads. It does not register the owner submitter.
Retire this patch when the selected upstream release registers that submitter
before admission and the deterministic user-effect regression passes unchanged.
Rollback is a source revert. No database migration or runtime activation belongs
to this source-only draft candidate.
