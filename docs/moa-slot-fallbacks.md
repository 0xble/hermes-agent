# Explicit MoA slot fallback chains

Each reference slot and the aggregator accept the same optional `fallback_models`
list. Entries are tried in order, at most four fallbacks, with explicit `provider`
and `model` strings and optional `reasoning_effort`. Chains cannot nest, repeat a
provider/model, use `auto`/`moa`, or contain credentials or transport overrides.
Provider configuration remains the sole source of transport and authentication.
Named `custom:<name>` slots retain their logical identity while freezing the
resolved `custom` transport, endpoint, API mode, and credential authority; restore
rejects any change to that physical identity.
Existing slot dictionaries without a chain remain valid.

```yaml
moa:
  presets:
    default:
      reference_models:
        - provider: xai-oauth
          model: grok-4.6
        - provider: anthropic
          model: claude-fable-5-1
          fallback_models:
            - provider: anthropic
              model: claude-opus-4-8
      aggregator:
        provider: custom
        model: gpt-5.6-sol
        # Optional fallback_models uses exactly the same list shape.
        # No aggregator fallback is selected by default.
```

Recovery advances on quota/rate limits, recognized model unavailability, provider
5xx/unavailability, or transport failures. Authentication/authorization errors,
invalid requests, context overflow, configuration/authority drift and programming
errors do not advance. Each candidate is selected once; existing bounded
same-route SDK/auxiliary transport retries still apply. MoA never substitutes the
generic main model. Exhausted references retain the existing loud/silent degraded
reference policy; exhausted aggregation raises instead of presenting raw reference
output as successful synthesis. Aggregator retries reuse the exact reference
results and prepared guidance, not another fan-out.

Streaming failures during creation or before the first chunk can advance; after
any chunk is exposed, the stream is not replayed on a different model. A later
consumer retry is a separate request under the existing acting-loop policy.

Council launch snapshots freeze **all** primary and fallback route identities,
including runtime authority. Durable metadata excludes credentials and resume
revalidates every candidate without consulting a changed preset registry.
Reference labels and accounting report the served route and label requested-to-
served changes. Aggregator pricing uses the served slot; trace metadata and
fallback display preserve the requested/served distinction. A provider's opaque
internal routing is not independently observable: served means the physical
route recorded by the auxiliary client, not proof of a vendor's hidden backend.

## Verification and upstream relationship

Run `scripts/run_tests.sh tests/agent/test_moa_reference_fallback.py
 tests/agent/test_moa_fallback_transport.py tests/run_agent/test_moa_loop_mode.py`.
The transport suite uses a disposable profile and local HTTP server through the
real resolver and auxiliary client, injects 429 responses, and verifies frozen
fallbacks, aggregation exhaustion and reference/aggregator isolation.

Prior art: upstream PR #56133 proposed ordered aggregator lists; this implementation
uses the same slot shape for references and aggregator rather than a separate
`fallback_aggregators` field. Its broad catch-and-return-raw-output degradation is
not adopted. PR #98033 / issue #97936 identified inaccurate reference route
attribution; requested/served reporting follows that approach while strict routes
prevent implicit generic-model recovery. No upstream commits were cherry-picked:
the older patches do not cover frozen delegated authority or both slot types.

Retire the fork implementation when upstream provides equivalent strict bounded
chains and frozen authority behavior. Rollback is a revert of this feature's
commit, after removing configured `fallback_models` through supported config tools.
Do not restore unrelated profile state or credentials.
