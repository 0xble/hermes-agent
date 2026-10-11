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
  Billing reads every declaration in `providers` and legacy `custom_providers`,
  without the picker's display-name/URL/model deduplication. Distinct aliases
  with identical display names remain separate billing evidence.
- Review repair: P1 on draft fork PR #478 demonstrated that picker deduplication
  hid an undeclared/metered alias behind an included alias at the same URL. Keep
  the existing normalization and enabled-entry rules, but evaluate unanimity
  before any display deduplication. This is a fork-only correction to the same
  configuration integration; the recorded upstream preflight remains applicable.
- Regression proof: `tests/agent/test_custom_subscription_cost.py` exercises both
  config shapes, named/bare routes, same-process profile A/B/A switching, and
  real main-turn SessionDB persistence. Shared display-name regressions cover
  both declaration orders, each config shape and mixed legacy/keyed entries,
  normalized trailing-slash URLs, disabled declarations, and positive reported
  costs. Existing
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

An explicit `subscription_included` declaration intentionally takes precedence
over a positive generic `raw_usage.cost`: subscription proxies may forward a
list-price token valuation that is not an additional user charge. The recorded
amount is marginal spend, not that valuation. Use this declaration only for a
route whose requests are covered by the subscription; an actually metered route
must omit it. Ambiguous bare routes do not qualify, so their positive reported
cost remains `actual` / `provider_cost_api`. Provider-profile-specific cost
results retain their existing precedence; this patch does not change them.

## P1 regression evidence

At `d20c7b4b2f276acdfa84f52f7e995e3769bac8c1`, the shared-display-name
regression was RED before changing the implementation. After reading the raw
route declarations, the same selection was GREEN:

```text
RED:   Summary: 1 files, 1 tests passed, 5 failed, 1 file FAILED (100% complete) in 1.7s (14 workers)
GREEN: Summary: 1 files, 6 tests passed, 0 failed (100% complete) in 1.3s (14 workers)
```

Both used `scripts/run_tests.sh tests/agent/test_custom_subscription_cost.py -k
shared_display_names` through the machine-wide heavy-slot gate and the private
worktree `.venv`. The full focused run (subscription, usage pricing,
provider-reported persistence, config, and non-mutating normalization) passed
274 tests with 0 failures and 4 Windows-only skips on macOS. Ruff and
`scripts/check_fork_patches.py` passed.

## Update and retirement

Preserve explicit opt-in, disabled-entry filtering, exact route identity, and
profile scoping. Retire this divergence once upstream exposes equivalent
configuration through the real usage path; preserve the metered persistence
regressions while adopting it. Mixed included/metered aliases at one URL must
continue to leave a bare `custom` route unknown.
