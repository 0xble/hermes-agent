# Managed-worker lineage log ordering

Fork patch identity: `managed-worker-lineage-order`.
Contract tests: `tests/gateway/test_managed_worker_lineage.py::test_worker_lineage_and_settlement`.

## Required behavior

Initialization failure must leave the admission unknown without submitting inference,
log `Managed worker lost: managed_worker_failed`, and never misclassify its error
frame as `invalid_worker_frame`. The test must observe both durable settlement and
the asynchronous log destination, without requiring either to flush the other.

## Diagnosis and scope

`agent.managed_worker.main` sends the sanitized error frame on initialization failure.
The gateway event-loop task in `execute_managed` enqueues its warning before
`tracked_write(lose_admission_worker)` commits the unknown admission. File I/O runs
later on `hermes_logging`'s `QueueListener` thread. The DB condition therefore does
not prove that `errors.log` contains the warning. This is a test ordering defect,
not evidence of a lost product warning or a defective security-audit lock.

Wait for the warning with the existing `async_wait_until` helper before preserving
both log assertions. Do not change product behavior, existing deadlines, or the
admission and no-inference assertions. A temporary deterministic RED/GREEN driver
holds the real listener's loss record until the test reads the pre-flush snapshot;
the old test fails that assertion and the corrected test retries the log condition.
The forced ordering failed once before the fix and passed five consecutive runs
after it, with file retries disabled. The native macOS managed-worker suites passed
97 tests across 40 files (37 platform skips); Linux coverage remains hosted.

## Upstream alignment and retirement

Guidance checked at upstream main `71736c9f32597bbca9d89cede5aa136e55946b2f`
(root/test AGENTS and CONTRIBUTING). Related upstream PRs #123704 and #77167 support
off-loop queued logging; #146198 and #143250 concern different worker-store and
Kanban-recovery defects. No equivalent lineage log-condition fix was found.
Retire this patch when the accepted upstream release waits for the same log
condition and the initialization scenario passes without the fork change.

Stack fit: existing Python/pytest integration test and `async_wait_until`; no new dependencies.
