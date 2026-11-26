# Fast failover on provider capacity

Load this unit when changing the primary API retry path for provider capacity or transport outages.

## Required behavior

When a configured fallback remains, an overloaded or unreachable primary gets at most one same-provider retry. A provider-declared long `Retry-After` is capped at 5 seconds for that retry, then the existing fallback activation path runs. This applies to provider overload/503/529, long 429 cooldowns, proxy `auth_unavailable`, and recognized EOF/TLS/connection-reset/refused transport failures. With no fallback, the existing `Retry-After` and bounded auto-recovery behavior remain unchanged. Real authentication failures, request validation, and context overflow remain on their existing paths.

Fallback restoration remains turn-scoped: `restore_primary_runtime()` probes and reinstalls the primary at the start of a later turn after its shared cooldown/reset gate permits it. This patch does not alter that cooldown or probe logic.

## Provenance and patches

Fork patch identity: `fast-failover-on-capacity`.

Fork-only behavior requested for provider outages; no upstream implementation was found on `origin/main`.

## Verification

Contract tests: `tests/agent/test_fast_failover_on_capacity.py`, `tests/agent/test_retry_wait_cap.py`, `tests/agent/test_billing_proxy_error_classification.py`.

## Retirement and rollback

Retire when a released upstream version provides equivalent one-short-retry failover behavior with no-fallback preservation. Revert the commits carrying `Fork-Patch: fast-failover-on-capacity`.
