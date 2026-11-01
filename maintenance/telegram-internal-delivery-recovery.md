# Telegram internal delivery recovery

Load for same-object Telegram polling-health changes or final-response delivery ledger settlement.

## Required behavior

A confirmed getUpdates recovery on a still-owned adapter promptly wakes the existing runtime ledger sweep for that adapter's exact transport profile. A failed final response whose refusal is persisted after that sweep also wakes replay when the delivery path is already healthy. Repeated health signals, stale generations, replaced adapters, delivered rows, permanent failures, ambiguous in-flight rows and another profile's rows must not cause duplicate delivery. Atomic ledger claims, retry bounds and recovered markers remain the authority; no schema change or automatic live-gateway promotion.

Adapter teardown cancellation must settle the same claim/send/refund operation,
even when cancelled repeatedly. Before dispatch, refund unsent rows without
spending an attempt; once dispatch starts, finish the send and its ledger
settlement rather than treating it as safely unsent. Do not start another row
once cancellation is observed, or repeat a refund after its worker has already
committed and a replacement has claimed that row.

## Provenance and patches

- Fork patch identity: `telegram-internal-delivery-recovery`.
- Origin: archived HERMES-109, `gateway-delivery.md`, commit `1619ea188a40` (`fix(telegram): recover stranded answers after polling health returns`). Adapted to the current fork's runtime sweep and profile registry, not the old timer implementation.
- Upstream main checked at `d0288be5b3330d2442e3907185b8e9d0958297bb`: still clears `_send_path_degraded` without waking replay. Related third-party PR [#105810](https://github.com/NousResearch/hermes-agent/pull/105810) adds the transition sweep, but its adapter-only tests do not cover persistence after the sweep or stale adapter ownership. Broader third-party [#107135](https://github.com/NousResearch/hermes-agent/pull/107135) addresses delivery races but is not imported wholesale; [#93440](https://github.com/NousResearch/hermes-agent/pull/93440) paces degraded send retries. No duplicate upstream PR opened.
- Independent approach: extend the confirmed health transition with one tracked, profile-scoped runner replay; compensate after a late failure write only if its live delivery adapter is healthy. The existing SQLite transaction and owner/profile claims remain authoritative rather than adding an outbox.

The runtime recovery mixin and its cancellation settlement primitive live in
`gateway/delivery_recovery.py`; `GatewayStartupMixin` inherits that owner without
a forwarding shim. The settlement primitive borrows
[PR #107135](https://github.com/NousResearch/hermes-agent/pull/107135)'s shielded,
non-repeating cancellation contract only. The current runner, startup replay,
flood worker and profile/ledger schema remain in place.

## Verification

Contract tests: `tests/gateway/test_telegram_internal_delivery_recovery.py`

`scripts/run_tests.sh tests/gateway/test_telegram_internal_delivery_recovery.py tests/gateway/test_delivery_ledger_producer.py tests/gateway/test_delivery_ledger.py tests/gateway/test_telegram_polling_health_confirmation.py -j 6 -q` covers health-to-ledger replay, profile isolation, stale adapter, duplicate health, late persistence, permanent failure and delivered-row exclusion.

`test_cancelled_recovery_settles_or_refunds_each_claim_once` covers cancellation
at the committed claim, resume clear, note admission, Bot API send, delivered
write, and both sides of a refund commit; repeat cancellation and successor
claims must not change settlement or cross profile ownership.
`test_sweep_failure_preserves_pending_teardown_cancellation` covers a shielded
sweep that raises after teardown cancellation: the original cancellation still
propagates, while an uncancelled sweep failure retains its zero-result behavior.
Both cases leave failed rows and sibling transports untouched.

## Rollback and retirement

Revert the scoped patch without resetting any ledger rows. Retire once an accepted upstream release supplies both same-object recovery and the late-write race guard with equivalent SQLite/profile regression evidence. Runtime adoption requires its separately authorized promotion path.
