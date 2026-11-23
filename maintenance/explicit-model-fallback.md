# Explicit model fallback

Fork patch identity: `explicit-model-fallback`.

An explicit session model or model-pinned delegation route can retry a different
credential or endpoint for the same model, but cannot pick an unrelated model
from a general fallback chain. If no eligible route remains, retain the causal
provider error. Defaults without a pin keep their configured fallback behavior.
Fallback results and logs distinguish the originally requested model, the
fallback target, the model that served the response (if any), and the reason.
User-facing notice deduplication for identical routes remains separate. Exception: a delegation route that declares its own non-null
`fallback_providers` owns that chain, so its child may fail over to those
declared models (`delegated-fallback-chain-ownership`).

Contract tests: `tests/agent/test_pinned_model_fallback.py`, `tests/tools/test_delegation_backends.py`, `tests/gateway/test_policy_fallback_wire.py`.

Contract tests: `tests/agent/test_pinned_model_fallback.py`,
    `tests/tools/test_delegation_backends.py`, and
    `tests/gateway/test_policy_fallback_wire.py`.

Proof: `tests/agent/test_pinned_model_fallback.py`,
`tests/tools/test_delegation_backends.py`, and
`tests/gateway/test_policy_fallback_wire.py`. The operator canary is documented
in `evals/provider_fallback/pinned_model_probe.md`. No proxy or cooldown policy
change is part of this patch. Retire when upstream enforces both the identity
and attribution contracts across the affected entry points.
