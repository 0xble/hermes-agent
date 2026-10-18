# Compression-Lock Deferred Turns: Coalesced Post-Compression Retry

This unit covers the fresh-turn compression lock and its deferred gateway inputs. Load it for compression-gate, pending-message drain, session-state, or related regression changes. It extends the root fork contract and has no independent schedule.

## Preserve

- A fresh turn never reads the pre-rotation transcript while compression holds the session lock.
- Gateway-authored wakes, including process notifications and goal or loop continuations, produce no user resend card and remain available for retry.
- Deferred inputs share one adapter pending slot, coalesce into one follow-up turn, and retain goal-continuation metadata.
- A user input gets at most one visible retry notice per compression-lock episode and is retried automatically after compression adoption.
- The existing bounded pending-drain backoff and compression-lock lease remain the recovery limits.

## Provenance And Patches

- **Identity and status:** `compression-lock-deferred`, active fork patch.
- **Source / fork refs:** Fresh-turn compression gating adopted from upstream PR #134244, with the coalescing and automatic-retry adaptation delivered in the maintained fork.
- **Surfaces:** `gateway/run_inbound.py`, `gateway/run_busy.py`, `gateway/session_state.py`, `locales/*.yaml`, and `tests/gateway/test_fresh_turn_compression_gate.py`.
- **Contribution route and rationale:** Fork maintenance. The incident required preserving internal wakes and changing the old resend-only behavior before upstream had the complete contract.
- **Links:** [Upstream PR #134244](https://github.com/NousResearch/hermes-agent/pull/134244), related [design commit `eebc2286fcdf`](https://github.com/0xble/hermes-agent/commit/eebc2286fcdf).
- **Adaptations:** The fork parks every deferred event in the adapter pending slot, merges repeated text and goal identity, suppresses cards for internal inputs, and resets the visible-notice latch when the lock episode ends.
- **Upstream disposition:** The upstream fresh-turn gate is the adopted source. The coalescing and internal-wake recovery behavior remains a fork adaptation until an upstream change covers the full contract.
- **Fork delivery:** Delivered on the maintained fork branch through the compression-lock deferred-turns change.

## Update

On every upstream sync touching the fresh-turn gate, pending-message drain, or compression lifecycle, compare the upstream behavior with this unit before rebasing. Retain the unit while the fork requires automatic coalescing or internal-wake preservation. Retire it only after released upstream behavior satisfies the complete contract and the focused regression is no longer fork-specific.

## Verify And Recover

Run the focused gateway tests and the exact-head fork gate before publication. Roll back the patch by reverting the commit carrying `Fork-Patch: compression-lock-deferred`, then rerun the focused tests and confirm the old resend-only behavior is intentionally restored.
