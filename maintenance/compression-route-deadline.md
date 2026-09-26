# Compression route deadline

Load this unit when changing compression worker cancellation, shared route deadlines, or stall fallback.

## Required behavior

A compression worker that unwinds because the shared route deadline expires is distinct from an explicit stop. A completed worker-side deadline result must still enter the configured fallback ladder, and if no fallback succeeds, an adopted concurrent transcript tail must be returned instead of the stale wrapper snapshot. Explicit stops and successful commits must not retry.

## Provenance and patches

Fork patch identity: `compression-route-deadline`.

Ported from archived HERMES-107 (archived commit `4dcb30f6bd62`) after reproducing the worker-first deadline race on `origin/main` and `upstream-live/main`. Upstream PR: [#123807](https://github.com/NousResearch/hermes-agent/pull/123807). Brian's earlier PR #102370 remains open and conflicting; this upstream-ready branch supersedes it with the current adaptation.

## Verification

`scripts/run_tests.sh tests/agent/test_compression_stall_fallback.py tests/agent/test_compression_attempt_lifecycle.py -j 6`

## Retirement and rollback

Retire after a released upstream version makes fallback independent of equal-deadline scheduling and preserves concurrent transcript tails. Roll back the patch commit and focused deadline regressions; no persistent-data migration is required.
