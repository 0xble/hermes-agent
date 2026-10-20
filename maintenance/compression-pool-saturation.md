# Compression pool saturation

Load this unit when changing compression-pool admission, transient compression signals, or attempt accounting in automatic compression callers.

## Required behavior

A compression pass refused only because the shared compression pool is full is a temporary defer, not a failed attempt. The refusal publishes `_compression_blocked_transient = "pool_saturated"`. Pre-API passes refund the compression attempt and leave the insufficient-progress blocker unarmed, turn-start passes stop for the turn without blocking later passes, and long-context overflow recovery refunds the attempt before re-entering its recovery loop. A later pass compresses once a slot frees. Summary-failure cooldown no-ops keep their existing semantics, so only saturation uses the narrower `compression_pool_saturated()` check. The deferred-turn notice names the busy pool instead of a failed attempt.

## Provenance and patches

Fork patch identity: `compression-pool-saturation`.

Fork-Patch-Backfill: a8645c8abecbc2e5edf24210a77a325248c97535; compression-pool-saturation

Reproduced on 2026-10-17: 4 concurrent long compressions held all 4 workers, and a fifth session spent its 3 attempts in about 35 seconds on `pool_saturated` refusals and ended `attempts_exhausted:3` without compressing. No matching upstream issue or PR was found.

## Verification

`scripts/run_tests.sh tests/agent/test_preflight_compression_pool_saturation.py tests/agent/test_over_window_compression_fail_closed.py tests/agent/test_compression_attempt_lifecycle.py tests/agent/test_long_context_pool_saturation.py`

## Retirement and rollback

Retire after a released upstream version treats pool saturation as a transient defer for every automatic caller. Roll back the patch commit and its regression. No persistent-data migration is required.
