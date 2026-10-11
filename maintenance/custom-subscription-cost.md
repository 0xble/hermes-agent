# Custom subscription billing

## Required behavior and prerequisites

An explicitly configured custom subscription route reports zero marginal cost
with `cost_status=included`, rather than inventing per-token subscription prices.
Configuration is authoritative, not the hostname or the absence of provider cost.
The provider-reported-cost unit remains the owner of metered response charges;
read [that unit](provider-reported-cost.md) when changing the priced path.

## Patch record

- Identity: `custom-subscription-cost`.
- Contribution route: fork-only integration of profile configuration with usage
  billing. No donor patch is used.
- Upstream disposition: read-only preflight against upstream
  `79b45d1647f6b5680768d851c4c406e8cc550a1e`; upstream PRs #136076
  (Nous subscription usage) and #130786 (pricing normalization) are related but
  do not provide custom-route subscription declarations. No upstream PR opened.
- Fork delivery: draft fork PR; publication and exact revision live in Git/PR.
- Adaptation: retain `billing_mode` in canonical provider-entry normalization;
  read the same enabled route identity and profile-scoped configuration as other
  provider metadata. Named custom aliases match their own entry and URL. Bare
  `custom` is included only when all enabled entries at that exact URL opt in.
- Regression proof: `tests/agent/test_custom_subscription_cost.py` exercises both
  config shapes, named/bare routes, same-process profile A/B/A switching, and
  real main-turn SessionDB persistence. Existing
  `tests/hermes_state/test_provider_reported_cost.py` exercises positive provider
  charges and nonzero main-turn actual cost persistence; nested/flat response
  parsing is covered by `tests/agent/test_usage_pricing.py`.

## Configuration decision

The previous runtime ignores provider-entry `billing_mode`, even when the key
already exists. This patch makes that declaration effective; it does not edit
any live profile or infer billing from loopback addresses.

For the authorized local subscription proxy routes, the parent can set:

```yaml
providers:
  codex-proxy:
    billing_mode: subscription_included
  claude-proxy:
    billing_mode: subscription_included
```

Merge these fields into the existing entries; preserve every credential and
other setting. Legacy `custom_providers` list entries support the same field.
These settings affect future recording only, not historical unknown rows.

## Update and retirement

Preserve explicit opt-in, disabled-entry filtering, exact route identity, and
profile scoping. Retire this divergence once upstream exposes equivalent
configuration through the real usage path; preserve the metered persistence
regressions while adopting it. Mixed included/metered aliases at one URL must
continue to leave a bare `custom` route unknown.
