# Plugin injection admission results

Fork patch identity: `plugin-injection-result`.

Upstream design: https://github.com/NousResearch/hermes-agent/pull/96179

Opt-in `await_dispatch=True` returns a correlated handle for admission, not execution.
The legacy Boolean scheduling result remains unchanged. A proven `queue_full`
refusal permits same-key retry; timeouts and cancellation after possible commit
are indeterminate and must never trigger replay under a new identity.

## Cross-repository dependency and landing order

The consumer is the separately maintained session-relay plugin in
[0xble/agents#2014](https://github.com/0xble/agents/pull/2014), branch
`fix/relay-admission-refusal`, tested head
`319717a78c2d46a70aec0c5aca58a0cbcf981c7e` (`319717a7`).

1. Land [hermes-agent#1089](https://github.com/0xble/hermes-agent/pull/1089)
   and make its admission-result API live first.
2. Only then land and activate agents#2014's session-relay consumer.

The optional `tests/gateway/test_plugin_injection_relay_receipt.py` proof needs
that consumer source/test harness on `PYTHONPATH`. A checkout without the
external harness, or with an older plugin lacking `retryable` / `correlation_id`
receipt fields, skips with an explicit dependency reason; that skip is not
cross-repository integration evidence. The test must run and pass against the
consumer branch before landing. No runtime activation is authorized by this note.

Contract tests: `tests/gateway/test_plugin_injection_result.py`.

Verification: `scripts/run_tests.sh tests/gateway/test_plugin_injection_result.py`.

Removal: drop this unit when upstream exposes equivalent admission receipts and
session-relay has migrated to that contract.
