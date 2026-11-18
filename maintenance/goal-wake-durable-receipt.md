# Goal wait wakes require durable admission

Fork patch identity: `goal-wake-durable-receipt`.

## Purpose

A parked goal must not lose its wait because the platform adapter created a task. That task
can still be refused by the session authority before any input was saved. The watcher counts
a continuation only after the authority commits it to the durable FIFO, together with an
acknowledgement of the exact goal instance and wait generation that caused the wake.

## Contract

- Keep timed, process and session waits intact on refusal, preparation failure, missing
  authority/provenance and capacity exhaustion. Retry on a later watcher scan with bounded backoff.
- Only the private `goal` synthetic capability permits a non-internal goal continuation through
  automation admission. A metadata dictionary alone is not authority.
- The producer identity names the goal instance and exact wait, not a random delivery UUID.
  A repeated admission returns the same durable row; a lost observer cannot split the row
  from its goal acknowledgement.
- A reset, fork, replacement goal or re-armed wait fences an old wake. `not_found` may retry
  only on the strict compression successor, never on the new owner of an old route key.
- Preserve unattended native producer provenance and the synthetic recovery contract of #1088:
  an admitted automation head later lost settles interrupted, never as a lost human input.
- No live-session recovery, barrier repair or runtime activation is part of this patch.

Contract tests: `tests/gateway/test_goal_wait_durable_admission.py`, `tests/gateway/test_goal_parked_idle_wake.py`, `tests/gateway/test_synthetic_native_admission.py`, `tests/gateway/test_automation_rebound_route.py`, `tests/gateway/test_autonomous_wake_pacing.py`; synthetic unattended recovery compatibility: `tests/gateway/test_unknown_synthetic_settles.py`, `tests/gateway/test_synthetic_provenance_retry.py`, `tests/gateway/test_loop_tick_native_settlement.py` (owned by #1088).

## Upstream recheck

This extends fork-owned goals and the native session-authority boundary, not the upstream
adapter wire protocol. The current upstream goal path does not supply this fork's goal wait
acknowledgement contract. Retire only when upstream offers the same transaction, private
producer capability, stable wake key, compression-only retry and recovery behavior, and the
contract tests pass against that implementation.
