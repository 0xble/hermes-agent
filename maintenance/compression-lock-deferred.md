# Compression-Deferred Turns

Extends the [fork contract](../MAINTENANCE.md). Load for gateway admission, hygiene turn-hold, pending-input draining, or compression-lock changes. No independent schedule.

## Preserve

- Compression owns its transcript until its fenced commit. Neither a fresh turn nor the turn that started hygiene may enter model preflight against that snapshot while the lock remains held.
- Retain accepted human and gateway inputs silently. Retry automatically in the owning adapter task with the existing bounded requeue backoff until compression commits or its leased lock expires. Keep the input in the pending slot during backoff for FIFO admission and shutdown recovery.
- Keep distinct events and their provenance, goal/loop fences, source pins, and outbox admission identities intact. An initiating input precedes arrivals queued during its hygiene wait.
- Defer goal/loop post-turn bookkeeping until execution. Slash controls remain available while model work is queued.

## Provenance and Patch

- **Identity:** `compression-lock-deferred`, maintained fork implementation.
- **Surfaces:** `gateway/run_busy.py`, `gateway/run_inbound.py`, `gateway/run_turn.py`, `gateway/platforms/base.py`, `tests/gateway/test_compression_deferred_retry.py`, `tests/gateway/test_compression_retry_lifecycle.py`, `tests/gateway/test_bot_loop_guard.py`, and `tests/gateway/test_session_race_guard.py`.
- **Failure mechanism:** watermark-fenced hygiene keeps running after the inline hold expires. Returning the old transcript allowed preflight to collide with that worker's durable lock and consume each input as another retry response, including gateway wakes.
- **Related upstream:** [NousResearch/hermes-agent#134244](https://github.com/NousResearch/hermes-agent/pull/134244), open when checked on 2026-10-22, covers the fresh-turn admission gap. It does not cover the initiating turn that starts hygiene after admission, and its external refusal requires a resend.
- **Adaptation:** apply one silent deferral mechanism at admission and after hygiene. Reuse existing FIFO ownership, adapter draining, requeue backoff, and compression leases. Do not merge events across control or identity boundaries. While a dispatched head awaits admission, refill an empty pending slot from older overflow before appending a new arrival.
- **Earlier fork candidate:** [0xble/hermes-agent#667](https://github.com/0xble/hermes-agent/pull/667) remained unmerged when this boundary was reconciled. Its older coalescing implementation predates the current FIFO provenance rules.
- **Distinct prior fix:** [0xble/hermes-agent#687](https://github.com/0xble/hermes-agent/pull/687) preserves proactive-prune replay versions and does not provide compression-locked input retry.

## Verify and Recover

Run `scripts/run_tests.sh tests/gateway/test_compression_deferred_retry.py tests/gateway/test_compression_retry_lifecycle.py tests/gateway/test_session_hygiene.py tests/gateway/test_session_hygiene_turnhold_adoption.py tests/gateway/test_busy_requeue_hot_loop.py tests/gateway/test_busy_session_ack.py tests/gateway/test_bot_loop_guard.py tests/gateway/test_session_race_guard.py -q`.

The regression owns a real SQLite compression lock, expires the initiating hygiene wait, admits 2 additional inputs before commit, and requires automatic drain with no retry notice, busy acknowledgment, or early model execution. Ordinary busy acknowledgments remain available when compression is not holding the session. Controls and event identity have separate coverage. A helper-only probe cannot establish gateway integration or live delivery.

There is no schema, configuration, prompt, dependency, or compression-threshold change. Adapter typing and processing-start hooks run once for the retained input and remain pending across retries. A retry does not restart those producers, complete delivery, or run a post-delivery callback. Runner model execution and progress producers start only after admission clears. Ingress rewrites, last-inbound stamps, and bot-budget admission run once, while authorization and cooldown checks remain live. Queue-mode debounce batches ordinary unheld human text. Before the shared FIFO admits a newer input, it transfers any older debounce burst and cancels that burst's timer without yielding. While compression owns the lane or its retry head remains visible, busy admission retains each later input in the existing FIFO instead of a debounce buffer. This preserves order across human text, external relay envelopes, and internal wakes, including the lock-acquire and lock-release/backoff boundaries. Never merge a retained input, including a photo with a later caption, or an internal wake or control/provenance boundary. Roll back only this unit's gateway changes and registration, preserving unrelated compression and queue fixes.

## Update

Retire the fork implementation only when released upstream behavior covers both admission and post-hygiene deferral, automatically retries retained inputs, preserves control provenance and ordering, and passes the same regression. Preserve the existing lock lease, commit fencing, and bounded requeue limits.
