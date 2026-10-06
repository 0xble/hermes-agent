# Compression route deadline

Load this unit when changing compression worker cancellation, shared route deadlines, or stall fallback.

## Required behavior

A compression worker that unwinds because the shared route deadline expires is distinct from an explicit stop. A completed worker-side deadline result must still enter the configured fallback ladder, and if no fallback succeeds, an adopted concurrent transcript tail must be returned instead of the stale wrapper snapshot. Explicit stops and successful commits must not retry. A cancelled worker that unwinds after another compression attempt claims the compressor must not persist its old stall cooldown over that newer attempt. The host records a recovered primary stall after its fallback commits, so suppressing stale writes does not lose the next-turn backoff. A cron script timeout uses the same deadline tree-kill contract: the probe records every real descendant and waits for bounded kernel convergence before asserting that each is gone or zombie. The wait does not relax the assertion or the script's two-second deadline; a living descendant after the settle bound remains a failure.

## Provenance and patches

Fork patch identity: `compression-route-deadline`.

Ported from archived HERMES-107 (archived commit `4dcb30f6bd62`) after reproducing the worker-first deadline race on `origin/main` and `upstream-live/main`. Upstream PRs [#123807](https://github.com/NousResearch/hermes-agent/pull/123807) and [#102370](https://github.com/NousResearch/hermes-agent/pull/102370) are closed as of 2026-09-26. Track the active comparable [#103088](https://github.com/NousResearch/hermes-agent/pull/103088), which addresses race-independent hard-ceiling fallback. An open comparable is not release acceptance, so retain the fork regressions until the selected release satisfies the full contract.

## Verification

`scripts/run_tests.sh tests/agent/test_compression_stall_fallback.py tests/agent/test_compression_attempt_lifecycle.py tests/agent/test_compression_stall_deterministic_fallback.py -j 6`

## Retirement and rollback

Retire after a released upstream version makes fallback independent of equal-deadline scheduling and preserves concurrent transcript tails. Roll back the patch commit and focused deadline regressions; no persistent-data migration is required.
