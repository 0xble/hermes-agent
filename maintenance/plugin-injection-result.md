# Plugin injection admission results

Fork patch identity: `plugin-injection-result`.

Upstream design: https://github.com/NousResearch/hermes-agent/pull/96179

Opt-in `await_dispatch=True` returns a correlated handle for admission, not execution.
The legacy Boolean scheduling result remains unchanged. A proven `queue_full`
refusal permits same-key retry; timeouts and cancellation after possible commit
are indeterminate and must never trigger replay under a new identity.

Consumer: the separately maintained session-relay plugin in 0xble/agents.

Verification: `scripts/run_tests.sh tests/gateway/test_plugin_injection_result.py`.

Removal: drop this unit when upstream exposes equivalent admission receipts and
session-relay has migrated to that contract.
