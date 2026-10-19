# Compression pool saturation

Load this unit when changing compression-pool admission, transient compression signals, or attempt accounting in pre-API and turn-start compression.

## Required behavior

A compression pass refused only because the shared compression pool is full is a temporary defer, not a failed attempt. The refusal publishes `_compression_blocked_transient = "pool_saturated"`. Pre-API passes refund the compression attempt and leave the insufficient-progress blocker unarmed, and turn-start passes stop for the turn without blocking later passes. A later pass compresses once a slot frees. Summary-failure cooldown no-ops keep their existing semantics, so only saturation uses the narrower `compression_pool_saturated()` check. The deferred-turn notice names the busy pool instead of a failed attempt.

## Provenance and patches

Fork patch identity: `compression-pool-saturation`.

Reproduced on 2026-10-17: 4 concurrent long compressions held all 4 workers, and a fifth session spent its 3 attempts in about 35 seconds on `pool_saturated` refusals and ended `attempts_exhausted:3` without compressing. No matching upstream issue or PR was found.

## Verification

`scripts/run_tests.sh tests/agent/test_preflight_compression_pool_saturation.py tests/agent/test_over_window_compression_fail_closed.py tests/agent/test_compression_attempt_lifecycle.py`

## Retirement and rollback

Retire after a released upstream version treats pool saturation as a transient defer for every automatic caller. Roll back the patch commit and its regression. No persistent-data migration is required.
