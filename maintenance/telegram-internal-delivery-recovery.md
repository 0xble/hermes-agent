# Telegram internal delivery recovery

Load for same-object Telegram polling-health changes or final-response delivery ledger settlement.

## Required behavior

A confirmed getUpdates recovery on a still-owned adapter promptly wakes the existing runtime ledger sweep for that adapter's exact transport profile. A failed final response whose refusal is persisted after that sweep also wakes replay when the delivery path is already healthy. Repeated health signals, stale generations, replaced adapters, delivered rows, permanent failures, ambiguous in-flight rows and another profile's rows must not cause duplicate delivery. Atomic ledger claims, retry bounds and recovered markers remain the authority; no schema change or automatic live-gateway promotion.

## Provenance and patches

- Fork patch identity: `telegram-internal-delivery-recovery`.
- Origin: archived HERMES-109, `gateway-delivery.md`, commit `1619ea188a40` (`fix(telegram): recover stranded answers after polling health returns`). Adapted to the current fork's runtime sweep and profile registry, not the old timer implementation.
- Upstream main checked at `d0288be5b3330d2442e3907185b8e9d0958297bb`: still clears `_send_path_degraded` without waking replay. Related third-party PR [#105810](https://github.com/NousResearch/hermes-agent/pull/105810) adds the transition sweep, but its adapter-only tests do not cover persistence after the sweep or stale adapter ownership. Broader third-party [#107135](https://github.com/NousResearch/hermes-agent/pull/107135) addresses delivery races but is not imported wholesale; [#93440](https://github.com/NousResearch/hermes-agent/pull/93440) paces degraded send retries. No duplicate upstream PR opened.
- Independent approach: extend the confirmed health transition with one tracked, profile-scoped runner replay; compensate after a late failure write only if its live delivery adapter is healthy. The existing SQLite transaction and owner/profile claims remain authoritative rather than adding an outbox.

## Verification

`scripts/run_tests.sh tests/gateway/test_telegram_internal_delivery_recovery.py tests/gateway/test_delivery_ledger_producer.py tests/gateway/test_delivery_ledger.py tests/gateway/test_telegram_polling_health_confirmation.py -j 6 -q` covers health-to-ledger replay, profile isolation, stale adapter, duplicate health, late persistence, permanent failure and delivered-row exclusion.

## Rollback and retirement

Revert the scoped patch without resetting any ledger rows. Retire once an accepted upstream release supplies both same-object recovery and the late-write race guard with equivalent SQLite/profile regression evidence. Runtime adoption requires its separately authorized promotion path.
