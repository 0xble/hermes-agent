# API authority FIFO wakeup at idle retirement

Fork patch identity: `api-authority-retry-flake`.

A WebSocket `prompt.submit` acknowledgement confirms durable admission, not execution. If an admission arrives while a FIFO drain is awaiting idle retirement, scheduling sets `rescan` on that still-running drain. Before the fix, the drain could return without consuming the signal, leaving the admission queued until another request woke it. This can happen even when a WebSocket viewer pins the live session: the API retirement path awaits a physical-target read before checking pins. With no viewer, a separate await on the durable busy snapshot could also let retirement evict the session after scheduling pinned it.

Commit `e2e80a4fa0644dd88b6b2c50dbb64452c6387d4c` (#1365) introduced these suspension points when it moved retirement reads off the event loop. Keep the reads off-loop; recheck loop-owned pins before eviction and have the active drain rescan if a new admission prevented retirement. The retry probe's existing timeout, retry behavior and exactly-once assertions remain unchanged. The `session.resume` pin for an in-progress history read is a separate change.

Contract tests: `tests/gateway/test_api_idle_retirement.py`, `tests/gateway/test_api_authority_retry.py`.

Retire this patch when upstream drains continue through a racing idle admission without requiring a third request. Revert the patch to roll back; it changes no persisted format.
